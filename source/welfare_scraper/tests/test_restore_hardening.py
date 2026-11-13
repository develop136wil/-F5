"""복원은 실패·취소해도 운영 DB를 건드리지 않고 모든 잠금을 반환해야 한다."""
import asyncio
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
import api_server
import content_filter
import database
from services import ai_coordinator, policy_sync, news_workflow_ownership as ownership


class TestRestoreHardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(ai_coordinator, '_job_lock_path', return_value=os.path.join(self.tmp.name, 'ai.lock')),
            patch('utils.get_app_data_dir', return_value=self.tmp.name),
            patch.object(database, 'DB_FILE', os.path.join(self.tmp.name, 'live.db')),
        ]
        for p in self.patches:
            p.start()
        ai_coordinator._reset_for_tests()
        policy_sync.release_maintenance_lock()

    def tearDown(self):
        ai_coordinator._reset_for_tests()
        policy_sync.release_maintenance_lock()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def assert_workflow_available(self):
        lease = ownership.acquire()
        self.assertIsNotNone(lease, 'restore leaked the workflow lease')
        lease.close()

    def probe_args(self):
        # Reuse the narrowly allowlisted child. Its only resource is this temp lock.
        return [sys.executable, '-B', '-m', 'tests.test_news_workflow_ownership',
                '--lease-probe', ownership._lock_path()]

    def probe_other_process(self, expected):
        child = subprocess.run(self.probe_args(), capture_output=True, text=True,
                               timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, expected, child.stderr)

    async def test_workflow_phase_gap_rejects_before_candidate_or_upload(self):
        from services import app_lifecycle
        lease = ownership.acquire()
        self.assertIsNotNone(lease)
        try:
            with patch.object(app_lifecycle, 'active_profile_jobs', 1), \
                    patch('tempfile.mkstemp') as candidate, \
                    patch.object(policy_sync, 'try_acquire_maintenance_lock', AsyncMock()) as maintenance:
                upload = UploadFile(file=io.BytesIO(b'bad'), filename='backup.db')
                with patch.object(upload, 'read', AsyncMock()) as read:
                    with self.assertRaises(HTTPException) as caught:
                        await api_server.restore_database(upload)
                    self.assertEqual(caught.exception.status_code, 409)
                candidate.assert_not_called()
                read.assert_not_awaited()
                maintenance.assert_not_awaited()
        finally:
            lease.close()
        self.assert_workflow_available()

    async def test_unknown_workflow_lock_fails_closed_before_candidate(self):
        with patch.object(ownership, 'acquire', return_value=None), \
                patch('tempfile.mkstemp') as candidate:
            with self.assertRaises(HTTPException) as caught:
                await api_server.restore_database(UploadFile(file=io.BytesIO(b'bad'), filename='backup.db'))
            self.assertEqual(caught.exception.status_code, 409)
            candidate.assert_not_called()

    async def test_other_process_workflow_rejects_restore_then_releases_normally(self):
        child = subprocess.Popen(self.probe_args() + ['--hold-until-stdin'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
            self.assertEqual(ready.strip(), 'LEASE_READY')
            with patch('tempfile.mkstemp') as candidate:
                with self.assertRaises(HTTPException) as caught:
                    await api_server.restore_database(UploadFile(file=io.BytesIO(b'bad'), filename='backup.db'))
                self.assertEqual(caught.exception.status_code, 409)
                candidate.assert_not_called()
        finally:
            _, error = await asyncio.to_thread(child.communicate, '\n', timeout=30)
        self.assertEqual(child.returncode, 0, error)
        self.assert_workflow_available()

    async def test_restore_upload_blocks_start_recovery_resume_and_other_process(self):
        from services import news_profiles_api
        entered, release = asyncio.Event(), asyncio.Event()
        reads = 0
        async def delayed_read(_size):
            nonlocal reads
            reads += 1
            if reads == 1:
                entered.set()
                await release.wait()
                return api_server._SQLITE_MAGIC
            return b''
        upload = UploadFile(file=io.BytesIO(), filename='backup.db')
        with patch.object(upload, 'read', side_effect=delayed_read), \
                patch.object(api_server, '_validate_restore_candidate', return_value=None), \
                patch.object(api_server, '_commit_database_restore', side_effect=lambda candidate, *_: os.remove(candidate)), \
                patch.object(news_profiles_api, '_execute', AsyncMock()) as workflow_db:
            task = asyncio.create_task(api_server.restore_database(upload))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                for action in (news_profiles_api.recover_idle_workflows,
                               lambda: news_profiles_api.start_default_condition(wait=True),
                               lambda: news_profiles_api.resume_workflow('synthetic')):
                    with self.assertRaises(HTTPException) as caught:
                        await action()
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertEqual(caught.exception.detail['error'], 'WORKFLOW_OWNER_UNAVAILABLE')
                workflow_db.assert_not_awaited()
                await asyncio.to_thread(self.probe_other_process, 3)
                with patch('tempfile.mkstemp') as second_candidate:
                    with self.assertRaises(HTTPException) as duplicate:
                        await api_server.restore_database(UploadFile(file=io.BytesIO(), filename='second.db'))
                    self.assertEqual(duplicate.exception.status_code, 409)
                    second_candidate.assert_not_called()
            finally:
                release.set()
                result = await task
            self.assertEqual(result['status'], 'success')
        self.assert_workflow_available()
        await asyncio.to_thread(self.probe_other_process, 0)

    async def test_upload_cancel_drains_current_read_before_removing_candidate_and_lease(self):
        entered, release = threading.Event(), threading.Event()
        def read_worker():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic read not released')
            return api_server._SQLITE_MAGIC
        async def read(_size):
            return await asyncio.to_thread(read_worker)
        upload = UploadFile(file=io.BytesIO(), filename='backup.db')
        with patch.object(upload, 'read', side_effect=read), \
                patch.object(api_server, '_validate_restore_candidate') as validate:
            task = asyncio.create_task(api_server.restore_database(upload))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertIsNone(ownership.acquire())
                self.assertTrue(any(n.startswith('welfare_restore_') for n in os.listdir(self.tmp.name)))
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            validate.assert_not_called()
        self.assert_workflow_available()
        self.assertFalse(any(n.startswith('welfare_restore_') for n in os.listdir(self.tmp.name)))

    async def test_validation_cancel_drains_reader_before_cleanup_and_lease_release(self):
        entered, release = threading.Event(), threading.Event()
        def validate(candidate):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic validator not released')
            self.assertTrue(os.path.exists(candidate))
            self.assertIsNone(ownership.acquire())
            return None
        with patch.object(api_server, '_validate_restore_candidate', side_effect=validate), \
                patch.object(api_server, '_commit_database_restore') as commit:
            task = asyncio.create_task(api_server.restore_database(
                UploadFile(file=io.BytesIO(api_server._SQLITE_MAGIC), filename='backup.db')))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertIsNone(ownership.acquire())
                self.assertTrue(ai_coordinator.status()['busy'])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            commit.assert_not_called()
        self.assert_workflow_available()
        self.assertFalse(any(n.startswith('welfare_restore_') for n in os.listdir(self.tmp.name)))

    async def test_validation_failure_returns_lease_without_commit(self):
        with patch.object(api_server, '_validate_restore_candidate', return_value='synthetic invalid schema'), \
                patch.object(api_server, '_commit_database_restore') as commit:
            with self.assertRaises(HTTPException) as caught:
                await api_server.restore_database(UploadFile(
                    file=io.BytesIO(api_server._SQLITE_MAGIC), filename='backup.db'))
            self.assertEqual(caught.exception.status_code, 400)
            commit.assert_not_called()
        self.assert_workflow_available()

    async def test_maintenance_and_legacy_busy_return_only_own_locks(self):
        self.assertTrue(await policy_sync.try_acquire_maintenance_lock('synthetic existing'))
        with self.assertRaises(HTTPException) as caught:
            await api_server.restore_database(UploadFile(file=io.BytesIO(), filename='backup.db'))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(policy_sync.maintenance_busy_message())
        self.assert_workflow_available()
        policy_sync.release_maintenance_lock()
        with patch.dict(api_server.SCRAPER_STATUS, {'is_running': True}):
            with self.assertRaises(HTTPException) as caught:
                await api_server.restore_database(UploadFile(file=io.BytesIO(), filename='backup.db'))
            self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assert_workflow_available()

    async def test_ai_busy_does_not_release_other_job_or_leak_workflow_lease(self):
        self.assertTrue(await ai_coordinator.try_start_job('synthetic', 'existing job'))
        try:
            with patch('tempfile.mkstemp') as candidate:
                with self.assertRaises(HTTPException) as caught:
                    await api_server.restore_database(UploadFile(file=io.BytesIO(), filename='backup.db'))
                self.assertEqual(caught.exception.status_code, 409)
                candidate.assert_not_called()
            self.assertTrue(ai_coordinator.status()['busy'])
            self.assertIsNone(policy_sync.maintenance_busy_message())
            self.assert_workflow_available()
        finally:
            ai_coordinator.finish_job('synthetic')

    async def test_release_order_keeps_workflow_last_even_if_lower_cleanup_fails(self):
        from services import scraper_service
        events = []
        lease = ownership.acquire()
        original_close = lease.close
        original_scrape = scraper_service._release_scrape_lock
        original_ai = ai_coordinator.finish_job
        original_maintenance = policy_sync.release_maintenance_lock
        def close_owner():
            events.append('workflow')
            original_close()
        def close_scrape(handle):
            events.append('scrape')
            original_scrape(handle)
            raise RuntimeError('synthetic cleanup failure')
        def close_ai(kind):
            events.append('ai')
            original_ai(kind)
        def close_maintenance():
            events.append('maintenance')
            original_maintenance()
        with patch.object(ownership, 'acquire', return_value=lease), \
                patch.object(lease, 'close', side_effect=close_owner), \
                patch.object(scraper_service, '_release_scrape_lock', side_effect=close_scrape), \
                patch.object(ai_coordinator, 'finish_job', side_effect=close_ai), \
                patch.object(policy_sync, 'release_maintenance_lock', side_effect=close_maintenance):
            with self.assertRaisesRegex(RuntimeError, 'synthetic cleanup failure'):
                await api_server.restore_database(UploadFile(file=io.BytesIO(b'bad'), filename='backup.db'))
        self.assertEqual(events, ['scrape', 'ai', 'maintenance', 'workflow'])
        self.assertFalse(ai_coordinator.status()['busy'])
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assert_workflow_available()

    async def test_cancel_during_real_rollback_retains_lease_and_original_data(self):
        entered, release = threading.Event(), threading.Event()
        original_rollback = api_server._restore_safety_backup_atomically
        original_init = database.init_db
        init_calls = 0
        def fail_init_once():
            nonlocal init_calls
            init_calls += 1
            if init_calls == 1:
                content_filter.clear_cache()
                self.assertEqual(content_filter.load_keywords(), ['backup-keyword'])
                raise RuntimeError('synthetic post-replace init failure')
            return original_init()
        def delayed_rollback(*args):
            entered.set()
            if not release.wait(30):
                raise TimeoutError('synthetic rollback not released')
            self.assertIsNone(ownership.acquire())
            return original_rollback(*args)
        with patch.object(database, '_all_conns', set()), patch.object(database, '_local', threading.local()):
            try:
                database.init_db()
                with database.get_connection() as conn:
                    conn.execute("INSERT INTO sites (name,url) VALUES ('backup-value','https://example.invalid')")
                    conn.execute('DELETE FROM keywords')
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('backup-keyword')")
                    conn.commit()
                candidate = os.path.join(self.tmp.name, 'rollback-candidate.db')
                database.backup_to(candidate)
                with database.get_connection() as conn:
                    conn.execute("UPDATE sites SET name='current-value'")
                    conn.execute('DELETE FROM keywords')
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('current-keyword')")
                    conn.commit()
                content_filter.clear_cache()
                self.assertEqual(content_filter.load_keywords(), ['current-keyword'])
                with open(candidate, 'rb') as source, \
                        patch.object(database, 'init_db', side_effect=fail_init_once), \
                        patch.object(api_server, '_restore_safety_backup_atomically', side_effect=delayed_rollback):
                    task = asyncio.create_task(api_server.restore_database(UploadFile(file=source, filename='backup.db')))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertIsNone(ownership.acquire())
                        self.assertTrue(ai_coordinator.status()['busy'])
                        await asyncio.to_thread(self.probe_other_process, 3)
                    finally:
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                with database.get_connection() as conn:
                    self.assertEqual(conn.execute('SELECT name FROM sites').fetchone()[0], 'current-value')
                self.assertEqual(content_filter.load_keywords(), ['current-keyword'])
                backups = [name for name in os.listdir(os.path.join(self.tmp.name, 'backups'))
                           if name.startswith('welfare_pre_restore_')]
                self.assertEqual(len(backups), 1)
                self.assertTrue(os.path.exists(candidate), 'uploaded source must be preserved')
                self.assertFalse(any(n.startswith(('welfare_restore_', 'welfare_recovery_'))
                                     for n in os.listdir(self.tmp.name)))
                self.assert_workflow_available()
                self.assertFalse(ai_coordinator.status()['busy'])
                self.assertIsNone(policy_sync.maintenance_busy_message())
            finally:
                content_filter.clear_cache()
                database.close_all_connections(strict=True)

    async def test_chunked_upload_preserves_bytes_and_bounded_reads(self):
        data = api_server._SQLITE_MAGIC + b'x' * 100
        upload = UploadFile(file=io.BytesIO(data), filename='backup.db')
        with tempfile.TemporaryFile() as output, patch.object(upload, 'read', wraps=upload.read) as reader:
            count = await api_server._stream_restore_upload(upload, output, 200, chunk_size=7)
            output.seek(0)
            self.assertEqual(output.read(), data)
            self.assertEqual(count, len(data))
            self.assertTrue(all(call.args == (7,) for call in reader.call_args_list))

    async def test_unknown_size_upload_stops_at_limit(self):
        upload = UploadFile(file=io.BytesIO(api_server._SQLITE_MAGIC + b'x' * 100), filename='backup.db')
        with tempfile.TemporaryFile() as output:
            with self.assertRaises(HTTPException):
                await api_server._stream_restore_upload(upload, output, 20, chunk_size=16)
            self.assertLessEqual(output.tell(), 20)

    async def test_invalid_upload_releases_locks_and_removes_temp(self):
        with patch.object(database, 'backup_to') as backup:
            with self.assertRaises(HTTPException) as error:
                await api_server.restore_database(UploadFile(file=io.BytesIO(b'bad'), filename='backup.db'))
            self.assertEqual(error.exception.status_code, 400)
            backup.assert_not_called()
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assertFalse(ai_coordinator.status()['busy'])
        self.assertFalse(any(name.startswith('welfare_restore_') for name in os.listdir(self.tmp.name)))
        self.assert_workflow_available()

    async def test_temp_creation_failure_releases_every_lock(self):
        with patch('tempfile.mkstemp', side_effect=OSError('disk full')):
            with self.assertRaises(HTTPException) as error:
                await api_server.restore_database(UploadFile(file=io.BytesIO(b'bad'), filename='backup.db'))
            self.assertEqual(error.exception.status_code, 500)
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assertFalse(ai_coordinator.status()['busy'])
        self.assert_workflow_available()

    async def test_other_scraper_rejects_restore_without_reading_upload(self):
        from services.scraper_service import _acquire_scrape_lock, _release_scrape_lock
        handle = _acquire_scrape_lock()
        self.assertIsNotNone(handle)
        try:
            upload = UploadFile(file=io.BytesIO(b'bad'), filename='backup.db')
            with patch.object(upload, 'read', new_callable=AsyncMock) as read:
                with self.assertRaises(HTTPException) as error:
                    await api_server.restore_database(upload)
                self.assertEqual(error.exception.status_code, 409)
                read.assert_not_called()
        finally:
            _release_scrape_lock(handle)
        self.assertFalse(ai_coordinator.status()['busy'])
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assert_workflow_available()

    async def test_cancelled_upload_releases_resources(self):
        upload = UploadFile(file=io.BytesIO(b''), filename='backup.db')
        with patch.object(upload, 'read', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await api_server.restore_database(upload)
        self.assertFalse(ai_coordinator.status()['busy'])
        self.assertIsNone(policy_sync.maintenance_busy_message())
        self.assertFalse(any(name.startswith('welfare_restore_') for name in os.listdir(self.tmp.name)))
        self.assert_workflow_available()

    async def test_real_sqlite_restore_keeps_safety_backup(self):
        with patch.object(database, '_all_conns', set()), patch.object(database, '_local', threading.local()):
            try:
                database.init_db()
                with database.get_connection() as conn:
                    conn.execute("INSERT INTO sites (name, url) VALUES ('before', 'https://example.org')")
                    conn.commit()
                candidate = os.path.join(self.tmp.name, 'candidate.db')
                database.backup_to(candidate)
                with database.get_connection() as conn:
                    conn.execute("UPDATE sites SET name='after'")
                    conn.commit()
                with open(candidate, 'rb') as source:
                    result = await api_server.restore_database(UploadFile(file=source, filename='backup.db'))
                self.assertEqual(result['status'], 'success')
                with database.get_connection() as conn:
                    self.assertEqual(conn.execute('SELECT name FROM sites').fetchone()[0], 'before')
                with sqlite3.connect(result['safety_backup']) as backup:
                    self.assertEqual(backup.execute('SELECT name FROM sites').fetchone()[0], 'after')
                backup.close()
                self.assert_workflow_available()
            finally:
                database.close_all_connections(strict=True)

    async def test_real_restore_invalidates_keyword_cache(self):
        with patch.object(database, '_all_conns', set()), patch.object(database, '_local', threading.local()):
            try:
                database.init_db()
                with database.get_connection() as conn:
                    conn.execute("DELETE FROM keywords")
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('복원전값')")
                    conn.commit()
                candidate = os.path.join(self.tmp.name, 'keyword-candidate.db')
                database.backup_to(candidate)
                with database.get_connection() as conn:
                    conn.execute("DELETE FROM keywords")
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('현재값')")
                    conn.commit()
                content_filter.clear_cache()
                self.assertEqual(content_filter.load_keywords(), ['현재값'])

                with open(candidate, 'rb') as source:
                    await api_server.restore_database(UploadFile(file=source, filename='backup.db'))
                self.assertEqual(content_filter.load_keywords(), ['복원전값'])
            finally:
                content_filter.clear_cache()
                database.close_all_connections(strict=True)

    async def test_shutdown_does_not_close_connections_of_active_job(self):
        with patch.dict(api_server.SCRAPER_STATUS, {'is_running': True}), \
             patch.object(database, 'close_all_connections') as close:
            await api_server._shutdown_resources()
            close.assert_not_called()
        api_server.STOP_EVENT.clear()
        api_server.BATCH_CANCEL_EVENT.clear()

    async def test_endpoint_cancel_does_not_release_locks_before_commit_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = api_server._commit_database_restore
        def delayed(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('worker not released')
            return original(*args)
        with patch.object(database, '_all_conns', set()), patch.object(database, '_local', threading.local()):
            try:
                database.init_db()
                with database.get_connection() as conn:
                    conn.execute('DELETE FROM keywords')
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('restored-keyword')")
                    conn.commit()
                candidate = os.path.join(self.tmp.name, 'candidate.db')
                database.backup_to(candidate)
                with database.get_connection() as conn:
                    conn.execute('DELETE FROM keywords')
                    conn.execute("INSERT INTO keywords(keyword) VALUES ('old-cached-keyword')")
                    conn.commit()
                content_filter.clear_cache()
                self.assertEqual(content_filter.load_keywords(), ['old-cached-keyword'])
                with open(candidate, 'rb') as source, \
                     patch.object(api_server, '_commit_database_restore', side_effect=delayed):
                    task = asyncio.create_task(api_server.restore_database(
                        UploadFile(file=source, filename='backup.db')))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertTrue(ai_coordinator.status()['busy'])
                        self.assertIsNotNone(policy_sync.maintenance_busy_message())
                        self.assertTrue(any(n.startswith('welfare_restore_') for n in os.listdir(self.tmp.name)))
                        self.assertIsNone(ownership.acquire())
                    finally:
                        release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertFalse(ai_coordinator.status()['busy'])
                self.assertIsNone(policy_sync.maintenance_busy_message())
                self.assertFalse(any(n.startswith('welfare_restore_') for n in os.listdir(self.tmp.name)))
                self.assert_workflow_available()
                self.assertEqual(content_filter.load_keywords(), ['restored-keyword'])
            finally:
                content_filter.clear_cache()
                database.close_all_connections(strict=True)

    async def test_shutdown_cancels_waiting_reservation(self):
        task = asyncio.create_task(asyncio.sleep(60))
        with patch.object(api_server, '_SCHEDULE_DEFER_TASK', task), \
             patch.object(api_server, '_SCHEDULE_DEFERRED_AT', 'waiting'), \
             patch.object(database, 'close_all_connections') as close:
            await api_server._shutdown_resources()
            self.assertTrue(task.cancelled())
            self.assertIsNone(api_server._SCHEDULE_DEFERRED_AT)
            close.assert_called_once()
        api_server.STOP_EVENT.clear()
        api_server.BATCH_CANCEL_EVENT.clear()


class TestConnectionCloseFailure(unittest.TestCase):
    def test_failed_close_is_retained_and_strict_mode_refuses(self):
        class ForeignThreadConnection:
            def close(self):
                raise sqlite3.ProgrammingError('created in another thread')
        connection = ForeignThreadConnection()
        with patch.object(database, '_all_conns', {connection}):
            with self.assertRaises(RuntimeError):
                database.close_all_connections(strict=True)
            self.assertIn(connection, database._all_conns)


if __name__ == '__main__':
    unittest.main()

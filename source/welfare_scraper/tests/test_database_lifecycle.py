"""Real worker connections, deterministic drain/cancellation, no production DB."""
import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import api_server
import content_filter
import database


class DatabaseLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(database, 'DB_FILE', os.path.join(self.tmp.name, 'live.db')),
            patch.object(database, '_all_conns', set()),
            patch.object(database, '_local', threading.local()),
        ]
        for item in self.patches:
            item.start()
        database.init_db()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites (name, url) VALUES ('old', 'https://example.org')")
            conn.commit()
        self.candidate = os.path.join(self.tmp.name, 'candidate.db')
        self.backup = os.path.join(self.tmp.name, 'safety.db')
        database.backup_to(self.candidate)
        with database.get_connection() as conn:
            conn.execute("UPDATE sites SET name='new'")
            conn.commit()

    def tearDown(self):
        database.close_all_connections(strict=True)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    @staticmethod
    def read_name():
        with database.get_connection() as conn:
            return conn.execute('SELECT name FROM sites').fetchone()[0]

    async def test_worker_connection_reopens_after_real_restore(self):
        # Same persistent worker must discard its closed thread-local connection.
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(self.read_name).result(5), 'new')
            await api_server._finish_restore_before_cancelling(
                self.candidate, database.DB_FILE, self.backup)
            self.assertEqual(pool.submit(self.read_name).result(5), 'old')
        self.assertEqual(self.read_name(), 'old')

    async def test_restore_drains_writer_and_blocks_new_access(self):
        entered, release, draining = threading.Event(), threading.Event(), threading.Event()
        def writer():
            with database.get_connection() as conn:
                conn.execute("UPDATE sites SET name='last-write'")
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test writer not released')
                # Nested access of an existing writer is permitted during draining.
                self.assertEqual(self.read_name(), 'last-write')
                conn.commit()
        original_wait = database._access_condition.wait
        def observed_wait(timeout=None):
            draining.set()
            return original_wait(timeout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer_future = pool.submit(writer)
            self.assertTrue(entered.wait(5))
            try:
                with patch.object(database._access_condition, 'wait', side_effect=observed_wait):
                    restore = pool.submit(api_server._commit_database_restore,
                                          self.candidate, database.DB_FILE, self.backup)
                    self.assertTrue(draining.wait(5))
                    with self.assertRaises(database.DatabaseMaintenanceError):
                        self.read_name()
                    self.assertFalse(restore.done())
                    release.set()
                    writer_future.result(5)
                    restore.result(5)
            finally:
                release.set()
        conn = sqlite3.connect(self.backup)
        try:
            self.assertEqual(conn.execute('SELECT name FROM sites').fetchone()[0], 'last-write')
        finally:
            conn.close()
        self.assertEqual(self.read_name(), 'old')

    async def test_drain_timeout_releases_gate_without_closing_active_connection(self):
        entered, release = threading.Event(), threading.Event()
        def reader():
            with database.get_connection() as conn:
                entered.set()
                release.wait(5)
                return conn.execute('SELECT name FROM sites').fetchone()[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(database.DatabaseMaintenanceError):
                    with database.exclusive_database(timeout=0):
                        self.fail('must not acquire while active')
                database.close_all_connections()  # shutdown must not close active reader
                self.assertEqual(self.read_name(), 'new')
            finally:
                release.set()
            self.assertEqual(future.result(5), 'new')

    async def test_failed_init_rolls_back_then_allows_queries(self):
        original = database.init_db
        calls = 0
        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                with database.get_connection() as conn:
                    conn.execute("UPDATE sites SET name='broken'")
                    conn.commit()
                raise RuntimeError('initialization failure')
            original()
        with patch.object(database, 'init_db', side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, 'initialization failure'):
                await api_server._finish_restore_before_cancelling(
                    self.candidate, database.DB_FILE, self.backup)
        self.assertEqual(self.read_name(), 'new')
        self.assertTrue(os.path.exists(self.backup))

    async def test_cancelled_commit_waits_for_worker_even_when_cancelled_twice(self):
        entered, release = threading.Event(), threading.Event()
        original = api_server._commit_database_restore
        def delayed(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('commit not released')
            return original(*args)
        with patch.object(api_server, '_commit_database_restore', side_effect=delayed):
            task = asyncio.create_task(api_server._finish_restore_before_cancelling(
                self.candidate, database.DB_FILE, self.backup))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.read_name(), 'old')

    async def test_sidecar_removal_failure_preserves_database(self):
        with patch.object(api_server, '_remove_db_sidecars', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                await api_server._finish_restore_before_cancelling(
                    self.candidate, database.DB_FILE, self.backup)
        self.assertEqual(self.read_name(), 'new')

    async def test_cancelled_run_db_still_counts_running_worker(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def work():
            try:
                entered.set()
                release.wait(5)
                return self.read_name()
            finally:
                finished.set()
        task = asyncio.create_task(database.run_db(work))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(database.DatabaseMaintenanceError):
                with database.exclusive_database(timeout=0):
                    self.fail('cancelled await must not release worker access')
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 5))
        # Drain joins the worker wrapper, not merely the body completion event.
        def drain():
            with database.exclusive_database():
                pass
        await asyncio.to_thread(drain)

    async def test_maintenance_response_is_retryable(self):
        response = await api_server.database_maintenance_error_handler(
            None, database.DatabaseMaintenanceError('복원 중'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '2')

    async def test_idle_shutdown_closes_worker_connections_and_allows_reopen(self):
        from services import ai_coordinator
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(self.read_name).result(5), 'new')
            with patch.object(ai_coordinator, 'busy_message', return_value=None), \
                 patch.object(api_server, '_SCHEDULE_DEFER_TASK', None), \
                 patch.dict(api_server.SCRAPER_STATUS, {'is_running': False}), \
                 patch.dict(api_server.BATCH_STATUS, {'is_running': False}), \
                 patch.dict(api_server.BOKJIRO_STATUS, {'is_running': False}):
                await api_server._shutdown_resources()
            self.assertFalse(database._all_conns)
            self.assertEqual(pool.submit(self.read_name).result(5), 'new')
        api_server.STOP_EVENT.clear()
        api_server.BATCH_CANCEL_EVENT.clear()

    async def test_strict_database_helpers_surface_failures(self):
        self.assertIsNone(database.execute_write("INSERT INTO table_that_does_not_exist VALUES (1)"))
        with self.assertRaises(sqlite3.OperationalError):
            database.execute_write(
                "INSERT INTO table_that_does_not_exist VALUES (1)", raise_on_error=True)
        with self.assertRaises(sqlite3.OperationalError):
            database.execute_query("SELECT * FROM table_that_does_not_exist", raise_on_error=True)

    async def test_keyword_save_failure_is_not_reported_as_success(self):
        with patch.object(database, 'get_connection', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                content_filter.save_keywords(['테스트'])

    async def test_readiness_requires_a_working_database(self):
        self.assertEqual((await api_server.get_readiness())['status'], 'ready')
        with patch.object(database, 'get_connection', side_effect=sqlite3.OperationalError('unavailable')):
            with self.assertRaises(api_server.HTTPException) as error:
                await api_server.get_readiness()
        self.assertEqual(error.exception.status_code, 503)

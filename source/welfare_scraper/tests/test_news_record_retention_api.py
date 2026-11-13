"""Retention route contracts with synthetic ASGI, settings, leases, and DB work.

No application startup, user settings write, database connection, or network is
needed. The service stub also permits running while retention is developed.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch, sentinel

from fastapi import FastAPI
from fastapi.testclient import TestClient

import services
from services import app_lifecycle, news_workflow_ownership, policy_sync


def _load_api():
    source = Path(__file__).resolve().parents[1] / 'services' / 'news_record_retention_api.py'
    spec = importlib.util.spec_from_file_location('_retention_api_contract_test', source)
    module = importlib.util.module_from_spec(spec)
    service = SimpleNamespace(configured_days=Mock(return_value=0), cleanup=Mock())
    # Restore the package attribute immediately; do not replace the actual
    # service module in sys.modules or pollute subsequent integration tests.
    with patch.object(services, 'news_record_retention', service, create=True):
        spec.loader.exec_module(module)
    return module


api = _load_api()


async def _inline_db(action, *args):
    return action(*args)


class TestNewsRecordRetentionApi(unittest.TestCase):
    def setUp(self):
        def start(patcher):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value
        start(patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN': 'retention-test-token'}))
        start(patch.object(app_lifecycle, 'shutdown_requested', False))
        self.configured = start(patch.object(api.retention, 'configured_days', return_value=0))
        self.cleanup = start(patch.object(api.retention, 'cleanup', return_value={'deleted_jobs': 2}))
        self.save = start(patch.object(api.utils, 'set_env_values'))
        self.connection = start(patch.object(api.database, 'get_connection'))
        self.connection.return_value.__enter__.return_value = sentinel.connection
        self.run_db = start(patch.object(api.database, 'run_db', side_effect=_inline_db))
        self.lease = Mock()
        self.acquire_lease = start(patch.object(news_workflow_ownership, 'acquire', return_value=self.lease))
        self.acquire_lock = start(patch.object(policy_sync, 'acquire_db_mutation_lock', new_callable=AsyncMock, return_value=True))
        self.release_lock = start(patch.object(policy_sync, 'release_maintenance_lock'))
        app = FastAPI()
        app.add_middleware(app_lifecycle.DesktopAdmissionMiddleware)
        app.include_router(api.router)
        self.client = TestClient(app)
        self.client.headers['X-Welfare-Session'] = 'retention-test-token'
        self.addCleanup(self.client.close)

    def post(self, **body):
        return self.client.post('/api/news/retention', json={'days': 7, 'confirmed': True, **body})

    def assert_no_write(self):
        self.save.assert_not_called()
        self.cleanup.assert_not_called()
        self.connection.assert_not_called()
        self.run_db.assert_not_called()

    def test_get_is_read_only_and_default_is_disabled(self):
        response = self.client.get('/api/news/retention')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'days': 0})
        self.configured.assert_called_once_with()
        self.assert_no_write()
        self.acquire_lease.assert_not_called()
        self.acquire_lock.assert_not_called()

    def test_get_reports_the_configured_choice(self):
        self.configured.return_value = 30
        self.assertEqual(self.client.get('/api/news/retention').json(), {'days': 30})
        self.assert_no_write()

    def test_all_three_choices_are_saved_but_disabled_does_not_open_db(self):
        for days in (0, 7, 30):
            with self.subTest(days=days):
                for mock in (self.save, self.cleanup, self.connection, self.lease.close, self.release_lock):
                    mock.reset_mock()
                response = self.post(days=days)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['days'], days)
                self.assertFalse(response.json()['cleanup_failed'])
                self.save.assert_called_once_with({'NEWS_RECORD_RETENTION_DAYS': str(days)})
                if days:
                    self.cleanup.assert_called_once_with(sentinel.connection, days)
                else:
                    self.cleanup.assert_not_called()
                    self.connection.assert_not_called()
                    self.assertIsNone(response.json()['cleanup'])
                self.release_lock.assert_called_once_with()
                self.lease.close.assert_called_once_with()

    def test_types_values_confirmation_and_extra_fields_fail_closed(self):
        cases = [({'days': value}, 422) for value in ('7', 7.0, True, None)]
        cases += [({'days': value}, 400) for value in (-1, 1, 14, 365)]
        cases += [({'confirmed': value}, 422) for value in ('true', 1, None)]
        cases += [({'confirmed': False}, 400), ({'unexpected': 'ignored?'}, 422)]
        for body, expected in cases:
            with self.subTest(body=body):
                response = self.post(**body)
                self.assertEqual(response.status_code, expected, response.text)
        for body in ({}, {'days': 7}, {'confirmed': True}):
            self.assertEqual(self.client.post('/api/news/retention', json=body).status_code, 422)
        self.assert_no_write()
        self.acquire_lease.assert_not_called()

    def test_bad_or_missing_desktop_token_cannot_read_or_write(self):
        for token in ('bad', ''):
            for method in ('GET', 'POST'):
                with self.subTest(token=token, method=method):
                    response = self.client.request(method, '/api/news/retention',
                        headers={'X-Welfare-Session': token}, json={'days': 7, 'confirmed': True})
                    self.assertEqual(response.status_code, 403)
        self.configured.assert_not_called()
        self.assert_no_write()
        self.acquire_lease.assert_not_called()

    def test_configuration_failure_never_starts_cleanup_and_releases_locks(self):
        self.save.side_effect = OSError('private-settings-path')
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private-settings-path', response.text)
        self.cleanup.assert_not_called()
        self.connection.assert_not_called()
        self.release_lock.assert_called_once_with()
        self.lease.close.assert_called_once_with()

    def test_cleanup_failure_reports_partial_success_after_saving(self):
        sequence = []
        self.save.side_effect = lambda values: sequence.append(('saved', values))
        def failed_cleanup(conn, days):
            sequence.append(('cleanup', days))
            raise RuntimeError('private-source-body')
        self.cleanup.side_effect = failed_cleanup
        response = self.post(days=30)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'days': 30, 'cleanup': None, 'cleanup_failed': True})
        self.assertEqual(sequence, [('saved', {'NEWS_RECORD_RETENTION_DAYS': '30'}), ('cleanup', 30)])
        self.assertNotIn('private-source-body', response.text)
        self.release_lock.assert_called_once_with()
        self.lease.close.assert_called_once_with()

    def test_connection_failure_also_preserves_saved_setting_and_reports_partial_success(self):
        self.connection.side_effect = OSError('private-database-path')
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'days': 7, 'cleanup': None, 'cleanup_failed': True})
        self.save.assert_called_once()
        self.cleanup.assert_not_called()
        self.lease.close.assert_called_once_with()
        self.release_lock.assert_called_once_with()

    def test_busy_workflow_lease_prevents_save_and_cleanup(self):
        self.acquire_lease.return_value = None
        self.assertEqual(self.post().status_code, 409)
        self.assert_no_write()
        self.acquire_lock.assert_not_called()
        self.release_lock.assert_not_called()
        self.lease.close.assert_not_called()

    def test_busy_maintenance_prevents_save_and_releases_workflow_lease(self):
        self.acquire_lock.return_value = False
        self.assertEqual(self.post().status_code, 409)
        self.assert_no_write()
        self.release_lock.assert_not_called()
        self.lease.close.assert_called_once_with()

    def test_lock_acquisition_error_releases_lease_without_writes(self):
        self.acquire_lock.side_effect = RuntimeError('private-lock-detail')
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private-lock-detail', response.text)
        self.assert_no_write()
        self.release_lock.assert_not_called()
        self.lease.close.assert_called_once_with()

    def test_worker_error_releases_both_locks(self):
        self.run_db.side_effect = RuntimeError('private-worker-detail')
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private-worker-detail', response.text)
        self.save.assert_not_called()
        self.cleanup.assert_not_called()
        self.release_lock.assert_called_once_with()
        self.lease.close.assert_called_once_with()

    def test_router_is_registered_in_application_source(self):
        source = (Path(__file__).resolve().parents[1] / 'api_server.py').read_text(encoding='utf-8')
        self.assertIn('from services.news_record_retention_api import router as news_retention_router', source)
        self.assertIn('app.include_router(news_retention_router)', source)


class TestNewsRecordRetentionCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_drains_database_worker_before_releasing_both_locks(self):
        entered, proceed = threading.Event(), threading.Event()
        events = []
        lease = Mock()
        lease.close.side_effect = lambda: events.append('lease released')
        async def run_db(action, *args):
            return await asyncio.to_thread(action, *args)
        def work(days):
            entered.set()
            if not proceed.wait(5):
                raise AssertionError('Test worker timed out')
            events.append('DB completed')
            return {'days': days, 'cleanup': None, 'cleanup_failed': False}
        task = None
        try:
            with patch.object(app_lifecycle, 'shutdown_requested', False), \
                 patch.object(news_workflow_ownership, 'acquire', return_value=lease), \
                 patch.object(policy_sync, 'acquire_db_mutation_lock', new_callable=AsyncMock, return_value=True), \
                 patch.object(policy_sync, 'release_maintenance_lock', side_effect=lambda: events.append('maintenance released')) as release, \
                 patch.object(api.database, 'run_db', side_effect=run_db), \
                 patch.object(api, '_save_and_cleanup', side_effect=work):
                task = asyncio.create_task(api.save_settings(api.RetentionRequest(days=7, confirmed=True)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()  # Repeated cancellation must not abandon DB work.
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                lease.close.assert_not_called()
                release.assert_not_called()
                proceed.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(events, ['DB completed', 'maintenance released', 'lease released'])
        finally:
            proceed.set()
            if task and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_shutdown_after_lock_acquisition_prevents_database_work(self):
        lease = Mock()
        with patch.object(app_lifecycle, 'shutdown_requested', True), \
             patch.object(news_workflow_ownership, 'acquire', return_value=lease), \
             patch.object(policy_sync, 'acquire_db_mutation_lock', new_callable=AsyncMock, return_value=True), \
             patch.object(policy_sync, 'release_maintenance_lock') as release, \
             patch.object(api.database, 'run_db', new_callable=AsyncMock) as run_db:
            with self.assertRaises(api.HTTPException) as caught:
                await api.save_settings(api.RetentionRequest(days=7, confirmed=True))
            self.assertEqual(caught.exception.status_code, 503)
            run_db.assert_not_called()
            release.assert_called_once_with()
            lease.close.assert_called_once_with()

    async def test_cancel_during_lock_acquisition_closes_only_workflow_lease(self):
        entered = asyncio.Event()
        async def acquire(label):
            entered.set()
            await asyncio.Event().wait()
        lease = Mock()
        with patch.object(news_workflow_ownership, 'acquire', return_value=lease), \
             patch.object(policy_sync, 'acquire_db_mutation_lock', side_effect=acquire), \
             patch.object(policy_sync, 'release_maintenance_lock') as release, \
             patch.object(api.database, 'run_db', new_callable=AsyncMock) as run_db:
            task = asyncio.create_task(api.save_settings(api.RetentionRequest(days=7, confirmed=True)))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            run_db.assert_not_called()
            release.assert_not_called()
            lease.close.assert_called_once_with()

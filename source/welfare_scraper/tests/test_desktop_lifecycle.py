import asyncio
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, BackgroundTasks
import api_server
import database
from services import app_lifecycle, ai_coordinator, policy_sync


class DesktopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = SimpleNamespace(should_exit=False)
        self.patches = [
            patch.dict(os.environ, WELFARE_DESKTOP_TOKEN='test-session', WELFARE_DESKTOP_INSTANCE='test-instance'),
            patch.object(api_server, '_DESKTOP_SERVER', self.server),
            patch.object(api_server, '_DESKTOP_SHUTDOWN_TASK', None),
            patch.object(api_server, 'scheduler', None),
            patch.object(api_server, '_SCHEDULE_DEFER_TASK', None),
            patch.object(app_lifecycle, 'shutdown_requested', False),
            patch.object(app_lifecycle, 'active_requests', 0),
            patch.object(app_lifecycle, 'active_scheduled_jobs', 0),
            patch.object(database, 'has_active_accesses', return_value=False),
            patch.object(policy_sync, 'maintenance_busy_message', return_value=None),
            patch.object(ai_coordinator, 'status', return_value={'job_kind': None}),
            patch.object(ai_coordinator, 'local_job_status', return_value={'job_kind': None, 'job_label': None}),
            patch.dict(api_server.SCRAPER_STATUS, is_running=False),
            patch.dict(api_server.BATCH_STATUS, is_running=False),
            patch.dict(api_server.BOKJIRO_STATUS, is_running=False),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        task = api_server._DESKTOP_SHUTDOWN_TASK
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        api_server.STOP_EVENT.clear()
        api_server.BATCH_CANCEL_EVENT.clear()
        for item in reversed(self.patches):
            item.stop()

    async def test_wrong_session_cannot_read_or_shutdown(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url='http://test') as client:
            for path in ('/api/desktop/status', '/api/health'):
                self.assertEqual((await client.get(path)).status_code, 403)
            self.assertEqual((await client.post('/api/desktop/shutdown', headers={'X-Welfare-Session': 'wrong'})).status_code, 403)
            self.assertFalse(app_lifecycle.shutdown_requested)
            response = await client.get('/api/desktop/status', headers={'X-Welfare-Session': 'test-session'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['instance'], 'test-instance')
            self.assertNotIn('test-session', response.text)

    async def test_idle_shutdown_sets_server_exit_without_killing_any_process(self):
        await api_server.desktop_shutdown()
        await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
        self.assertTrue(self.server.should_exit)
        self.assertTrue(api_server.STOP_EVENT.is_set())

    async def test_shutdown_rejects_new_requests_and_jobs(self):
        app_lifecycle.active_requests = 1
        await api_server.desktop_shutdown()
        self.assertFalse(await ai_coordinator.try_start_job('test', 'test'))
        self.assertFalse(await policy_sync.try_acquire_maintenance_lock('test'))
        self.assertFalse(await api_server._scheduled_scrape_job())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url='http://test', headers={'X-Welfare-Session': 'test-session'}) as client:
            self.assertEqual((await client.get('/api/health')).status_code, 503)
            self.assertEqual((await client.post('/api/db/restore')).status_code, 503)
            self.assertEqual((await client.get('/api/desktop/status')).status_code, 200)
        self.assertFalse(self.server.should_exit)
        app_lifecycle.active_requests = 0
        await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
        self.assertTrue(self.server.should_exit)

    async def test_maintenance_or_worker_prevents_exit_until_finished(self):
        with patch.object(policy_sync, 'maintenance_busy_message', return_value='DB 복원') as maintenance, \
             patch.object(database, 'has_active_accesses', return_value=True) as active:
            await api_server.desktop_shutdown()
            await asyncio.sleep(0)
            self.assertFalse(self.server.should_exit)
            maintenance.return_value = None
            await asyncio.sleep(0)
            self.assertFalse(self.server.should_exit)
            active.return_value = False
            await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
        self.assertTrue(self.server.should_exit)

    async def test_background_response_is_counted_until_finished(self):
        sample = FastAPI()
        sample.add_middleware(app_lifecycle.DesktopAdmissionMiddleware)
        entered, release = asyncio.Event(), asyncio.Event()
        async def background():
            entered.set()
            await release.wait()
        @sample.post('/api/work')
        async def work(tasks: BackgroundTasks):
            tasks.add_task(background)
            return {'ok': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sample), base_url='http://test', headers={'X-Welfare-Session': 'test-session'}) as client:
            request = asyncio.create_task(client.post('/api/work'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(app_lifecycle.active_requests, 1)
                await api_server.desktop_shutdown()
                await asyncio.sleep(0)
                self.assertFalse(self.server.should_exit)
            finally:
                release.set()
                await request
        await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
        self.assertEqual(app_lifecycle.active_requests, 0)
        self.assertTrue(self.server.should_exit)

    async def test_repeated_shutdown_does_not_replace_drain_task(self):
        app_lifecycle.active_scheduled_jobs = 1
        await api_server.desktop_shutdown()
        task = api_server._DESKTOP_SHUTDOWN_TASK
        await api_server.desktop_shutdown()
        self.assertIs(task, api_server._DESKTOP_SHUTDOWN_TASK)
        self.assertFalse(self.server.should_exit)
        app_lifecycle.active_scheduled_jobs = 0
        await asyncio.wait_for(task, 2)

    async def test_status_explains_shutdown_stage(self):
        status = await api_server.desktop_status()
        self.assertEqual(status['shutdown_stage'], '')
        app_lifecycle.active_requests = 1
        await api_server.desktop_shutdown()
        status = await api_server.desktop_status()
        self.assertEqual(status['shutdown_stage'], '진행 중인 작업·저장 완료 대기')
        app_lifecycle.active_requests = 0
        await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
        status = await api_server.desktop_status()
        self.assertEqual(status['shutdown_stage'], '통신 연결 정리 중')

    async def test_heartbeat_does_not_read_quota_and_samples_job_once(self):
        with patch.object(ai_coordinator, 'status', side_effect=AssertionError('quota I/O on heartbeat')), \
             patch.object(ai_coordinator, 'local_job_status', return_value={'job_kind': 'analysis', 'job_label': 'AI 분석'}) as local:
            status = await api_server.desktop_status()
            self.assertTrue(status['busy'])
            self.assertEqual(status['job'], 'AI 분석')
            local.assert_called_once_with()
            await api_server.desktop_shutdown()
            await asyncio.sleep(0)
            self.assertFalse(self.server.should_exit, 'local AI job still protects shutdown')
            local.return_value = {'job_kind': None, 'job_label': None}
            await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
            self.assertTrue(self.server.should_exit)

    async def test_browser_dev_cannot_invoke_shutdown(self):
        with patch.dict(os.environ, WELFARE_DESKTOP_TOKEN=''):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url='http://test') as client:
                self.assertEqual((await client.post('/api/desktop/shutdown')).status_code, 409)

    async def test_parent_disappearance_requests_drain_and_closes_watch(self):
        with patch.dict(os.environ, WELFARE_DESKTOP_PARENT_PID='123'), \
             patch('services.desktop_parent.ParentWatch') as watch:
            watch.return_value.alive.return_value = False
            await api_server._watch_desktop_parent()
            watch.return_value.close.assert_called_once()
            self.assertTrue(app_lifecycle.shutdown_requested)
            await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)

    async def test_scheduler_pause_failure_still_starts_drain(self):
        with patch.object(api_server, 'scheduler') as scheduler:
            scheduler.pause.side_effect = RuntimeError('not running')
            await api_server.desktop_shutdown()
            await asyncio.wait_for(api_server._DESKTOP_SHUTDOWN_TASK, 2)
            self.assertTrue(self.server.should_exit)


class LocalJobSnapshotTests(unittest.TestCase):
    def test_local_snapshot_has_no_quota_or_disk_dependency(self):
        with patch.object(ai_coordinator, '_JOB_KIND', 'analysis'), \
             patch.object(ai_coordinator, '_JOB_LABEL', 'AI 분석'), \
             patch.object(ai_coordinator, '_roll_daily_state', side_effect=AssertionError('quota')), \
             patch.object(ai_coordinator, '_refresh_external_busy', side_effect=AssertionError('file lock')), \
             patch('builtins.open', side_effect=AssertionError('file access')):
            self.assertEqual(ai_coordinator.local_job_status(), {'job_kind': 'analysis', 'job_label': 'AI 분석'})


class PortSafetyTests(unittest.TestCase):
    def test_busy_port_keeps_original_listener_alive(self):
        with api_server.bind_server_socket(0) as first:
            port = first.getsockname()[1]
            with self.assertRaises(OSError):
                api_server.bind_server_socket(port)
            first.setblocking(True)
            first.settimeout(2)
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                accepted, _ = first.accept()
                accepted.close()
        with api_server.bind_server_socket(port):
            pass

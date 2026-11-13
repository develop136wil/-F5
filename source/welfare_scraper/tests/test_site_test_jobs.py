"""Offline diagnostics: short requests, retained results, lifecycle and safety."""
import ast
import asyncio
import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from services import site_test_jobs as module
from services import ai_coordinator, app_lifecycle


SITE = dict(id=11, name='교육 소식', url='https://example.invalid/news/list.do',
            selectors={'list_item': '.row', 'title': 'a'}, max_pages=3,
            registration_status='rules_ready')


class SiteJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.jobs = module.SiteTestJobs()
        self.start = patch.object(ai_coordinator, 'try_start_job', new_callable=AsyncMock).start()
        self.start.return_value = True
        self.finish = patch.object(ai_coordinator, 'finish_job').start()
        self.addCleanup(patch.stopall)
        self.addAsyncCleanup(self.jobs.shutdown)

    async def settle(self):
        await self.jobs._task
        await asyncio.sleep(0)

    async def test_response_does_not_wait_for_browser_and_result_survives_reentry(self):
        release = asyncio.Event()
        async def runner(site):
            await release.wait()
            return {'status': 'success', 'count': 10, 'items': [{'title': '새 소식'}]}
        first = await asyncio.wait_for(self.jobs.start(SITE, runner), timeout=.2)
        self.assertEqual(first['status'], 'running')
        self.assertTrue(self.jobs.status(SITE)['running'])
        first['message'] = 'client mutation'
        self.assertNotEqual(self.jobs.status(SITE)['message'], first['message'])
        release.set()
        await self.settle()
        status = self.jobs.status(SITE)
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['result']['count'], 10)
        self.assertFalse(status['running'])
        self.finish.assert_called_once_with('site_test')

    async def test_duplicate_posts_reuse_one_job_and_other_site_is_busy(self):
        async def runner(site):
            await asyncio.sleep(.03)
            return {'count': 0}
        a, b = await asyncio.gather(self.jobs.start(SITE, runner), self.jobs.start(SITE, runner))
        self.assertEqual(a['task_id'], b['task_id'])
        with self.assertRaises(module.SiteTestBusy):
            await self.jobs.start({**SITE, 'id': 12}, runner)
        self.start.assert_awaited_once()
        await self.settle()

    async def test_deleted_or_changed_site_cannot_inherit_old_results(self):
        await self.jobs.start(SITE, AsyncMock(return_value={'count': 0}))
        await self.settle()
        self.assertEqual(self.jobs.status({**SITE, 'url': SITE['url']+'?new=1'})['status'], 'idle')
        self.assertEqual(self.jobs.status({**SITE, 'max_pages': 1})['status'], 'idle')
        self.jobs.invalidate(SITE['id'])
        self.assertEqual(self.jobs.status(SITE)['status'], 'idle')

    async def test_deleted_running_site_result_does_not_reappear(self):
        release = asyncio.Event()
        async def runner(site):
            await release.wait()
            return {'count': 1}
        await self.jobs.start(SITE, runner)
        self.jobs.invalidate(SITE['id'])
        release.set()
        await self.settle()
        self.assertEqual(self.jobs.status(SITE)['status'], 'idle')

    async def test_empty_success_failure_and_timeout_are_distinct(self):
        for runner, expected in ((AsyncMock(return_value={'count': 0}), 'completed'),
                                 (AsyncMock(side_effect=RuntimeError('private URL/token')), 'failed'),
                                 (AsyncMock(side_effect=TimeoutError()), 'timed_out')):
            with self.subTest(expected=expected):
                await self.jobs.start(SITE, runner)
                await self.settle()
                result = self.jobs.status(SITE)
                self.assertEqual(result['status'], expected)
                self.assertNotIn('private', str(result))

    async def test_browser_timeout_cancels_work_and_releases_after_cleanup(self):
        cleaned = asyncio.Event()
        async def runner(site):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch.object(module, 'TEST_TIMEOUT_SECONDS', .01):
            await self.jobs.start(SITE, runner)
            await self.settle()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.jobs.status(SITE)['status'], 'timed_out')
        self.finish.assert_called_once()

    async def test_shutdown_keeps_lease_until_browser_cleanup_finishes(self):
        entered, cleanup_started, cleanup_release = (asyncio.Event() for _ in range(3))
        async def runner(site):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
        await self.jobs.start(SITE, runner)
        await entered.wait()
        shutdown = asyncio.create_task(self.jobs.shutdown())
        await cleanup_started.wait()
        self.jobs.request_shutdown()  # repeated desktop/lifespan signals are harmless
        self.finish.assert_not_called()
        cleanup_release.set()
        await shutdown
        self.assertEqual(self.jobs.status(SITE)['status'], 'cancelled')
        self.finish.assert_called_once()

    async def test_cancel_before_coroutine_enters_releases_lease(self):
        await self.jobs.start(SITE, AsyncMock())
        await self.jobs.shutdown()
        self.finish.assert_called_once()
        self.assertFalse(self.jobs.status(SITE)['running'])

    async def test_recent_results_bounded_to_64(self):
        for index in range(70):
            await self.jobs.start({**SITE, 'id': index}, AsyncMock(return_value={'count': 0}))
            await self.settle()
        self.assertEqual(len(self.jobs._records), 64)
        self.assertEqual(self.jobs.status({**SITE, 'id': 0})['status'], 'idle')
        self.assertEqual(self.jobs.status({**SITE, 'id': 69})['status'], 'completed')

    async def test_existing_restore_or_shutdown_admission_creates_no_job(self):
        self.start.return_value = False
        with patch.object(ai_coordinator, 'busy_message', return_value='다른 작업 진행 중'):
            with self.assertRaises(module.SiteTestBusy):
                await self.jobs.start(SITE, AsyncMock())
        self.assertFalse(self.jobs._records)
        self.finish.assert_not_called()


class SiteJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_coordinator_lease_blocks_restore_and_desktop_exit(self):
        jobs = module.SiteTestJobs()
        release = asyncio.Event()
        async def runner(site):
            await release.wait()
            return {'count': 0}
        with (patch.object(ai_coordinator, '_acquire_process_lock', return_value=object()),
              patch.object(ai_coordinator, '_release_process_lock'),
              patch.object(app_lifecycle, 'shutdown_requested', False)):
            try:
                await jobs.start(SITE, runner)
                self.assertEqual(ai_coordinator.local_job_status()['job_kind'], 'site_test')
                self.assertFalse(await ai_coordinator.try_start_job('db_restore', 'DB 복원'))
                release.set()
                await jobs._task
                await asyncio.sleep(0)
                self.assertIsNone(ai_coordinator.local_job_status()['job_kind'])
                self.assertTrue(await ai_coordinator.try_start_job('db_restore', 'DB 복원'))
            finally:
                await jobs.shutdown()
                ai_coordinator.finish_job('db_restore')


def api_functions():
    """Compile actual endpoint bodies without importing/starting the app."""
    source = Path(__file__).parents[1] / 'api_server.py'
    names = {'get_sites', 'test_site', 'site_test_status', '_run_site_test',
             '_settle_restore_operation', 'desktop_shutdown'}
    tree = ast.parse(source.read_text(encoding='utf-8'))
    body = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    namespace = dict(app=FastAPI(), HTTPException=HTTPException, asyncio=asyncio, json=json)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


class SiteTestApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = api_functions()
        self.sites = [copy.deepcopy(SITE)]
        self.jobs = module.SiteTestJobs()
        patch.object(module, 'jobs', self.jobs).start()
        patch.object(ai_coordinator, 'try_start_job', AsyncMock(return_value=True)).start()
        patch.object(ai_coordinator, 'finish_job').start()
        self.addCleanup(patch.stopall)
        self.addAsyncCleanup(self.jobs.shutdown)
        self.api['utils'] = SimpleNamespace(load_sites=lambda: copy.deepcopy(self.sites))
        self.api['database'] = SimpleNamespace(run_db=AsyncMock(side_effect=lambda fn: fn()))

    async def test_missing_site_is_404_and_list_restores_status(self):
        with self.assertRaises(HTTPException) as caught:
            await self.api['test_site'](999)
        self.assertEqual(caught.exception.status_code, 404)
        self.api['_run_site_test'] = AsyncMock(return_value={'count': 10})
        first = await self.api['test_site'](SITE['id'])
        await self.jobs._task
        listed = await self.api['get_sites']()
        status = await self.api['site_test_status'](SITE['id'])
        self.assertEqual(listed[0]['test_task'], status)
        self.assertEqual(first['task_id'], status['task_id'])
        self.assertEqual(status['result']['count'], 10)
        self.assertNotIn('test_task', self.sites[0])

    async def test_rules_ready_failure_is_not_zero_success_and_partial_keeps_items(self):
        from services.scraper_service import ScraperService
        for state, items in (('failed', []), ('finished', []), ('partial', [{'title': '새 소식'}])):
            with self.subTest(state=state), patch.object(ScraperService, 'test_single_site',
                    AsyncMock(return_value={'state': state, 'items': items})) as scrape:
                if state == 'failed':
                    with self.assertRaises(RuntimeError):
                        await self.api['_run_site_test'](SITE)
                else:
                    result = await self.api['_run_site_test'](SITE)
                    self.assertEqual(result['count'], len(items))
                scrape.assert_awaited_once_with(SITE, with_status=True)

    async def test_registration_update_rechecks_identity_in_same_transaction(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE sites(id INTEGER PRIMARY KEY, name TEXT, url TEXT, '
                     'base_url TEXT, selectors TEXT, max_pages INTEGER, registration_status TEXT)')
        basic = {**SITE, 'base_url': '', 'registration_status': 'needs_check'}
        conn.execute('INSERT INTO sites VALUES (?,?,?,?,?,?,?)',
                     (basic['id'], basic['name'], basic['url'], '', json.dumps(basic['selectors']),
                      basic['max_pages'], 'needs_check'))
        conn.commit()
        @contextmanager
        def connection():
            yield conn
        self.api['database'].get_connection = connection
        for changed in ('none', 'selectors', 'deleted'):
            with self.subTest(changed=changed):
                conn.execute('UPDATE sites SET selectors=?, registration_status=?',
                             (json.dumps(SITE['selectors']), 'needs_check'))
                conn.commit()
                async def probe(site, **kwargs):
                    self.assertTrue(kwargs['raise_errors'])
                    if changed == 'selectors':
                        conn.execute('UPDATE sites SET selectors=?', ('{"list_item":".new"}',))
                        conn.commit()
                    if changed == 'deleted':
                        self.jobs.invalidate(site['id'])
                    return True
                with patch('services.site_registration.probe_site', side_effect=probe):
                    await self.jobs.start(basic, self.api['_run_site_test'])
                    await self.jobs._task
                    await asyncio.sleep(0)
                value = conn.execute('SELECT registration_status FROM sites').fetchone()[0]
                self.assertEqual(value, 'basic_verified' if changed == 'none' else 'needs_check')

    async def test_desktop_shutdown_signals_diagnostic_cancellation_immediately(self):
        lifecycle = SimpleNamespace(token=lambda: 'session', shutdown_requested=False)
        self.api.update(app_lifecycle=lifecycle, _DESKTOP_SERVER=object(),
                        STOP_EVENT=asyncio.Event(), BATCH_CANCEL_EVENT=asyncio.Event(),
                        scheduler=None, _SCHEDULE_DEFER_TASK=None,
                        _drain_desktop_shutdown=AsyncMock())
        with patch.object(self.jobs, 'request_shutdown') as cancel:
            response = await self.api['desktop_shutdown']()
            self.assertTrue(response['accepted'])
            self.assertTrue(lifecycle.shutdown_requested)
            cancel.assert_called_once()
            await self.api['_DESKTOP_SHUTDOWN_TASK']


class SingleSiteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_page_copy_and_cleanup_on_success_and_start_failure(self):
        from services.scraper_service import ScraperService
        for fails in (False, True):
            with self.subTest(fails=fails):
                engine = MagicMock(start=AsyncMock(side_effect=RuntimeError() if fails else None),
                                   close=AsyncMock(), scrape_site=AsyncMock(return_value=[]))
                original = copy.deepcopy(SITE)
                with patch('services.scraper_service.ScraperEngine', return_value=engine):
                    if fails:
                        with self.assertRaises(RuntimeError):
                            await ScraperService().test_single_site(original)
                    else:
                        await ScraperService().test_single_site(original)
                        self.assertEqual(engine.scrape_site.call_args.args[0]['max_pages'], 1)
                        self.assertIs(engine.scrape_site.call_args.args[0]['news_article_only'], True)
                self.assertEqual(original, SITE)
                engine.close.assert_awaited_once()

    async def test_cancellation_during_start_still_closes_browser(self):
        from services.scraper_service import ScraperService
        entered = asyncio.Event()
        async def start():
            entered.set()
            await asyncio.Event().wait()
        engine = MagicMock(start=start, close=AsyncMock())
        with patch('services.scraper_service.ScraperEngine', return_value=engine):
            task = asyncio.create_task(ScraperService().test_single_site(SITE))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        engine.close.assert_awaited_once()


class SiteProbeErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_default_is_preserved_but_test_retains_error_type(self):
        from services.site_registration import probe_site
        for error in (TimeoutError('offline timeout'), RuntimeError('offline SSL failure')):
            with self.subTest(error=type(error).__name__), patch(
                    'services.news_attachments.validate_public_attachment_url',
                    AsyncMock(side_effect=error)):
                self.assertFalse(await probe_site(SITE))
                with self.assertRaises(type(error)):
                    await probe_site(SITE, raise_errors=True)


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_available_edge_never_installs_another_browser(self):
        from scraper_engine import ScraperEngine
        browser = MagicMock(close=AsyncMock())
        playwright = MagicMock(stop=AsyncMock())
        playwright.chromium.launch = AsyncMock(return_value=browser)
        with (patch('scraper_engine.async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))),
              patch.object(ScraperEngine, '_install_browser', AsyncMock()) as install):
            engine = ScraperEngine()
            await engine.start()
            install.assert_not_awaited()
            playwright.chromium.launch.assert_awaited_once_with(headless=True, channel='msedge')
            await engine.close()

    async def test_cancelled_installer_is_terminated_and_reaped(self):
        from scraper_engine import ScraperEngine
        entered = asyncio.Event()
        async def communicate():
            entered.set()
            await asyncio.Event().wait()
        process = MagicMock(returncode=None, communicate=communicate, wait=AsyncMock(return_value=0))
        with patch('scraper_engine.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(ScraperEngine._install_browser())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_slow_termination_is_killed_and_reaped(self):
        from scraper_engine import ScraperEngine
        process = MagicMock(returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError()),
                            wait=AsyncMock(side_effect=[TimeoutError(), 0]))
        with patch('scraper_engine.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await ScraperEngine._install_browser()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)

    async def test_browser_close_timeout_does_not_skip_driver_cleanup(self):
        from scraper_engine import ScraperEngine
        engine = ScraperEngine()
        engine.browser = MagicMock(close=AsyncMock(side_effect=TimeoutError()))
        engine.playwright = MagicMock(stop=AsyncMock())
        await engine.close()
        engine.playwright.stop.assert_awaited_once()

"""Synthetic DB, OS lease and run-once exit contracts. No real service calls."""
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from services import news_workflow_ownership as ownership


class WorkflowLeaseTests(unittest.TestCase):
    def test_exclusive_released_and_stale_file_is_not_a_live_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                ownership, '_lock_path', return_value=os.path.join(directory, 'owner.lock')):
            first = ownership.acquire()
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(ownership.acquire())
            finally:
                first.close()
                first.close()  # callback/failure cleanup is idempotent
            second = ownership.acquire()
            self.assertIsNotNone(second)
            second.close()
            self.assertTrue(os.path.isfile(os.path.join(directory, 'owner.lock')))

    def test_unknown_lock_access_fails_closed(self):
        with patch('builtins.open', side_effect=PermissionError('synthetic')):
            self.assertIsNone(ownership.acquire())

    def test_task_close_waits_for_cancellation_acknowledgement_pin(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                ownership, '_lock_path', return_value=os.path.join(directory, 'owner.lock')):
            lease = ownership.acquire()
            with lease.pin():
                lease.close()
                lease.close()
                self.assertIsNone(ownership.acquire())
            following = ownership.acquire()
            self.assertIsNotNone(following)
            following.close()

    def test_other_process_and_abnormal_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'owner.lock')
            with patch.object(ownership, '_lock_path', return_value=path):
                lease = ownership.acquire()
                args = [sys.executable, '-B', '-m', 'tests.test_news_workflow_ownership',
                        '--lease-probe', path]
                options = dict(capture_output=True, text=True, timeout=30,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    blocked = subprocess.run(args, **options)
                    self.assertEqual(blocked.returncode, 3, blocked.stderr)
                finally:
                    lease.close()
                crashed = subprocess.run(args + ['--crash-owned-child'], **options)
                self.assertEqual(crashed.returncode, 23, crashed.stderr)
                recovered = ownership.acquire()
                self.assertIsNotNone(recovered)
                recovered.close()


class WorkflowAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import database
        from services import app_lifecycle, news_profiles_api as api
        self.database, self.api = database, api
        database.close_all_connections()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database_path = patch.object(database, 'DB_FILE', os.path.join(self.temp.name, 'test.db'))
        database_path.start(); self.addCleanup(database_path.stop)
        shutdown = patch.object(app_lifecycle, 'shutdown_requested', False)
        shutdown.start(); self.addCleanup(shutdown.stop)
        database.init_db()

    async def asyncTearDown(self):
        tasks = list(self.api._WORKFLOW_TASKS.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)  # completion callbacks return transferred leases
        self.api._WORKFLOW_TASKS.clear()
        self.database.close_all_connections()

    def make_default(self, state='ready'):
        from services import news_default_condition as defaults, news_profile_store as store
        from services.news_profile import NewsProfile
        from services.news_profile_workflow import create_workflow, _transition
        with self.database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES(1,'시험','https://example.invalid','{}')")
            defaults.save(conn, NewsProfile(defaults.PROFILE_ID, 1, '시험', '전국',
                          site_ids=('1',), semantic_text='복지 지원 소식'), 0)
            run = store.prepare_run(conn, defaults.PROFILE_ID, expected_revision=1,
                                    sites=[dict(id=1, name='시험', url='https://example.invalid')])
            settings = self.api._runtime_workflow_settings()
            workflow = create_workflow(conn, run, settings=settings)
            if state != 'ready':
                _transition(conn, workflow, 'running', 'collecting')
                if state == 'waiting':
                    _transition(conn, workflow, 'waiting', 'collecting')
            conn.commit()
            return workflow, run

    async def test_busy_or_unknown_never_recovers_or_creates(self):
        from fastapi import HTTPException
        for lease in (None,):
            with patch.object(ownership, 'acquire', return_value=lease), \
                    patch.object(self.api, '_execute', AsyncMock()) as db:
                for action in (self.api.recover_idle_workflows,
                               lambda: self.api.start_default_condition(wait=True),
                               lambda: self.api.resume_workflow('fixture')):
                    with self.assertRaises(HTTPException) as caught:
                        await action()
                    self.assertEqual(caught.exception.detail['error'], 'WORKFLOW_OWNER_UNAVAILABLE')
                db.assert_not_awaited()

    async def test_idle_gui_recovery_releases_lease_for_scheduled_run(self):
        self.make_default('running')
        await self.api.recover_idle_workflows()
        lease = ownership.acquire()
        self.assertIsNotNone(lease)
        lease.close()

    async def test_live_workflow_blocks_startup_recovery_and_other_launch_until_done(self):
        from fastapi import HTTPException
        workflow, _ = self.make_default()
        lease = await self.api._acquire_workflow_ownership()
        entered = asyncio.Event()
        async def blocked(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.api, '_run_workflow', side_effect=blocked):
            self.api._launch_workflow(workflow, ownership=lease)
            task = self.api._WORKFLOW_TASKS[workflow]
            await entered.wait()
            with self.assertRaises(HTTPException):
                await self.api.recover_idle_workflows()
            with self.assertRaises(HTTPException):
                await self.api.start_default_condition(wait=True)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        next_lease = ownership.acquire()
        self.assertIsNotNone(next_lease)
        next_lease.close()

    async def test_cancel_before_task_first_step_releases_owner(self):
        lease = await self.api._acquire_workflow_ownership()
        with patch.object(self.api, '_run_workflow', AsyncMock()) as work:
            self.api._launch_workflow('fixture', ownership=lease)
            task = self.api._WORKFLOW_TASKS['fixture']
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            work.assert_not_awaited()
        next_lease = ownership.acquire()
        self.assertIsNotNone(next_lease)
        next_lease.close()

    async def test_cancel_during_recovery_keeps_owner_until_db_settles(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(_action):
            entered.set()
            await release.wait()
            return 0
        with patch.object(self.api, '_execute', side_effect=delayed):
            task = asyncio.create_task(self.api._acquire_workflow_ownership())
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertIsNone(ownership.acquire())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        next_lease = ownership.acquire()
        self.assertIsNotNone(next_lease)
        next_lease.close()

    async def test_recovery_error_releases_owner_without_launch(self):
        with patch.object(self.api, '_execute', AsyncMock(side_effect=RuntimeError('synthetic'))):
            with self.assertRaises(RuntimeError):
                await self.api._acquire_workflow_ownership()
        lease = ownership.acquire()
        self.assertIsNotNone(lease)
        lease.close()

    async def test_remote_live_owner_cannot_be_recovered_or_cancelled(self):
        from fastapi import HTTPException
        from services.news_profile_workflow import get_workflow
        workflow, _ = self.make_default('running')
        remote = ownership.acquire()
        try:
            with self.assertRaises(HTTPException):
                await self.api.recover_idle_workflows()
            with self.assertRaises(HTTPException):
                await self.api.cancel_workflow(workflow)
            with self.database.get_connection() as conn:
                self.assertEqual(get_workflow(conn, workflow)['state'], 'running')
        finally:
            remote.close()

    async def assert_admission_mutation_drains(self, mutation):
        workflow, _ = self.make_default('waiting')
        entered, release = threading.Event(), threading.Event()
        original = self.api._execute
        delayed_once = False
        async def delayed(action):
            nonlocal delayed_once
            code = getattr(action, '__code__', None)
            names = () if code is None else code.co_names + code.co_freevars
            if not delayed_once and (action.__name__ == mutation or mutation in names):
                delayed_once = True
                def worker(conn):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('synthetic worker timeout')
                    return action(conn)
                return await original(worker)
            return await original(action)
        actions = {
            'synchronize_sites': lambda: self.api.start_default_condition(wait=True),
            'prepare_resume': lambda: self.api.resume_workflow(workflow),
            'cancel_if_ready': lambda: self.api.cancel_workflow(workflow),
        }
        with patch.object(self.api, '_execute', side_effect=delayed), \
                patch('ai_classifier.get_client', return_value=object()), \
                patch('services.ai_coordinator.status', return_value={}):
            task = asyncio.create_task(actions[mutation]())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertIsNone(ownership.acquire())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        lease = ownership.acquire()
        self.assertIsNotNone(lease)
        lease.close()
        self.assertFalse(self.api._WORKFLOW_TASKS)

    async def test_cancelled_site_synchronization_drains_before_owner_release(self):
        await self.assert_admission_mutation_drains('synchronize_sites')

    async def test_cancelled_resume_preparation_drains_before_owner_release(self):
        await self.assert_admission_mutation_drains('prepare_resume')

    async def test_cancelled_idle_cancel_drains_before_owner_release(self):
        await self.assert_admission_mutation_drains('cancel_if_ready')

    async def assert_stage_commit_drains(self, state):
        from services.news_profile_workflow import run_workflow, get_workflow, _transition
        from services.news_analysis_worker import db_call
        workflow, _ = self.make_default('waiting')
        entered, release = threading.Event(), threading.Event()
        delayed_once = False
        async def delayed(fn, *args, **kwargs):
            nonlocal delayed_once
            if fn is _transition and args[1] == state and not delayed_once:
                delayed_once = True
                def worker(conn, *worker_args, **worker_kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('synthetic worker timeout')
                    return fn(conn, *worker_args, **worker_kwargs)
                return await db_call(worker, *args, **kwargs)
            return await db_call(fn, *args, **kwargs)
        async def synthetic(workflow_id, **kwargs):
            return await run_workflow(workflow_id, **kwargs, call_db=delayed,
                collector=AsyncMock(return_value={'state':'finished','analysis_required':False}),
                executor=AsyncMock())
        with patch('ai_classifier.get_client', return_value=object()), \
                patch('services.ai_coordinator.status', return_value={}), \
                patch.object(self.api, '_run_workflow', side_effect=synthetic):
            await self.api.resume_workflow(workflow)
            task = self.api._WORKFLOW_TASKS[workflow]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertIsNone(ownership.acquire())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        with self.database.get_connection() as conn:
            self.assertEqual(get_workflow(conn, workflow)['state'],
                             'complete' if state == 'complete' else 'cancelled')
        lease = ownership.acquire()
        self.assertIsNotNone(lease)
        lease.close()

    async def test_cancelled_stage_transition_drains_before_owner_release(self):
        await self.assert_stage_commit_drains('running')

    async def test_cancel_after_completion_commit_preserves_terminal_result(self):
        await self.assert_stage_commit_drains('complete')

    async def test_cancel_api_keeps_lease_after_task_done_until_ack_commit(self):
        from services.news_profile_workflow import get_workflow
        workflow, _ = self.make_default('waiting')
        lease = await self.api._acquire_workflow_ownership()
        entered, release = threading.Event(), threading.Event()
        original = self.api._execute
        async def blocked(*_args, **_kwargs):
            await asyncio.Event().wait()
        async def delayed(action):
            code = getattr(action, '__code__', None)
            if code and 'cancel_if_ready' in code.co_names + code.co_freevars:
                def worker(conn):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('synthetic worker timeout')
                    return action(conn)
                return await original(worker)
            return await original(action)
        with patch.object(self.api, '_run_workflow', side_effect=blocked), \
                patch.object(self.api, '_execute', side_effect=delayed):
            self.api._launch_workflow(workflow, ownership=lease)
            cancel = asyncio.create_task(self.api.cancel_workflow(workflow))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertNotIn(workflow, self.api._WORKFLOW_TASKS)
                self.assertIsNone(ownership.acquire())
                cancel.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(cancel.done())
                self.assertIsNone(ownership.acquire())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await cancel
        with self.database.get_connection() as conn:
            self.assertEqual(get_workflow(conn, workflow)['state'], 'cancelled')
        following = ownership.acquire()
        self.assertIsNotNone(following)
        following.close()

    async def assert_waiter_final_read_keeps_lease(self, cancel):
        import api_server
        from fastapi import HTTPException, UploadFile
        from services.news_profile_workflow import run_workflow
        import io
        workflow, _ = self.make_default('waiting')
        entered, release = threading.Event(), threading.Event()
        original = self.api._execute
        async def synthetic(workflow_id, **kwargs):
            return await run_workflow(workflow_id, **kwargs,
                collector=AsyncMock(return_value={'state':'finished','analysis_required':False}),
                executor=AsyncMock())
        async def delayed(action):
            code = getattr(action, '__code__', None)
            if code and action.__name__ == '<lambda>' and 'get_workflow' in code.co_freevars:
                def worker(conn):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('synthetic final read not released')
                    self.assertIsNone(ownership.acquire())
                    return action(conn)
                return await original(worker)
            return await original(action)
        with patch('ai_classifier.get_client', return_value=object()), \
                patch('services.ai_coordinator.status', return_value={}), \
                patch.object(self.api, '_run_workflow', side_effect=synthetic), \
                patch.object(self.api, '_execute', side_effect=delayed):
            waiter = asyncio.create_task(self.api.start_default_condition(wait=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertNotIn(workflow, self.api._WORKFLOW_TASKS)
                self.assertIsNone(ownership.acquire())
                with patch('tempfile.mkstemp') as candidate:
                    with self.assertRaises(HTTPException) as caught:
                        await api_server.restore_database(UploadFile(file=io.BytesIO(), filename='backup.db'))
                    self.assertEqual(caught.exception.status_code, 409)
                    candidate.assert_not_called()
                if cancel:
                    for _ in range(2):
                        waiter.cancel()
                        await asyncio.sleep(0)
                    self.assertFalse(waiter.done())
                    self.assertIsNone(ownership.acquire())
            finally:
                release.set()
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await waiter
                else:
                    self.assertEqual((await waiter)['state'], 'complete')
        following = ownership.acquire()
        self.assertIsNotNone(following)
        following.close()

    async def test_waiter_final_read_retains_lease_after_task_completion(self):
        await self.assert_waiter_final_read_keeps_lease(cancel=False)

    async def test_cancelled_waiter_final_read_drains_before_lease_release(self):
        await self.assert_waiter_final_read_keeps_lease(cancel=True)

    async def assert_original_run_finishes(self, initial_state):
        import api_server
        from services.news_profile_workflow import get_workflow, run_workflow
        workflow, run_id = self.make_default(initial_state)
        async def synthetic(workflow_id, **kwargs):
            # Real workflow state transitions and DB persistence, no transports.
            return await run_workflow(workflow_id, **kwargs,
                collector=AsyncMock(return_value={'state': 'finished', 'registered': 1}),
                executor=AsyncMock(return_value={'state': 'pass_finished', 'jobs_processed': 1}))
        with patch('ai_classifier.get_client', return_value=object()), \
                patch.object(self.api, '_run_workflow', side_effect=synthetic), \
                patch('services.ai_coordinator.status', return_value={}):
            code = await api_server._run_once_scrape()
        self.assertEqual(code, api_server.RUN_ONCE_SUCCESS)
        with self.database.get_connection() as conn:
            result = get_workflow(conn, workflow)
            self.assertEqual(result['state'], 'complete')
            self.assertEqual(result['run_id'], run_id)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_profile_workflows').fetchone()[0], 1)

    async def test_run_once_recovers_running_original_run_to_completion(self):
        await self.assert_original_run_finishes('running')

    async def test_run_once_recovers_ready_original_run_to_completion(self):
        await self.assert_original_run_finishes('ready')

    async def test_run_once_resumes_normal_waiting_without_new_run(self):
        await self.assert_original_run_finishes('waiting')


class RunOnceExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_outcomes_are_not_all_success(self):
        import api_server
        from services import news_profiles_api as api
        for state, expected in [('complete', 0), ('waiting', 3), ('failed', 1),
                                ('cancelled', 130), ('ready', 1), ('running', 1), (None, 1)]:
            with self.subTest(state=state), patch.object(api, 'start_default_condition',
                    AsyncMock(return_value={'state': state})), \
                    patch('services.scraper_service.ScraperService') as legacy:
                self.assertEqual(await api_server._run_once_scrape(), expected)
                legacy.assert_not_called()

    async def test_not_ready_busy_failure_and_cancel_have_distinct_exit_codes(self):
        import api_server
        from fastapi import HTTPException
        from services import news_profiles_api as api
        for error, expected in [(HTTPException(409, 'setup'), 2),
                (HTTPException(409, {'error':'WORKFLOW_OWNER_UNAVAILABLE'}), 3),
                (HTTPException(503, 'maintenance'), 3), (RuntimeError('synthetic'), 1),
                (asyncio.CancelledError(), 130)]:
            with self.subTest(code=expected), patch.object(api, 'start_default_condition',
                    AsyncMock(side_effect=error)), patch('services.scraper_service.ScraperService') as legacy:
                self.assertEqual(await api_server._run_once_scrape(), expected)
                legacy.assert_not_called()

    async def test_legacy_empty_success_failure_and_busy_are_preserved(self):
        import api_server
        from services import news_profiles_api as api
        for admitted, error, expected in [(True, None, 0), (True, RuntimeError('synthetic'), 1), (False, None, 3)]:
            service = AsyncMock()
            service.run_scraper_async = AsyncMock(return_value=[], side_effect=error)
            with self.subTest(code=expected), patch.object(api, 'start_default_condition', AsyncMock(return_value=None)), \
                    patch.object(api_server.database, 'run_db', AsyncMock(return_value=[{'id':1}])), \
                    patch('services.ai_coordinator.try_start_job', AsyncMock(return_value=admitted)), \
                    patch('services.ai_coordinator.finish_job') as finish, \
                    patch('services.scraper_service.ScraperService', return_value=service):
                self.assertEqual(await api_server._run_once_scrape(), expected)
                self.assertEqual(finish.call_count, int(admitted))

    async def test_legacy_without_sites_is_not_ready(self):
        import api_server
        from services import news_profiles_api as api
        with patch.object(api, 'start_default_condition', AsyncMock(return_value=None)), \
                patch.object(api_server.database, 'run_db', AsyncMock(return_value=[])), \
                patch('services.ai_coordinator.try_start_job', AsyncMock()) as start:
            self.assertEqual(await api_server._run_once_scrape(), 2)
            start.assert_not_awaited()

    async def test_legacy_metadata_distinguishes_zero_partial_busy_and_unready(self):
        import api_server
        from services import news_profiles_api as api
        for state, expected in [('completed', 0), ('partial_failure', 1), ('failed', 1),
                                ('busy', 3), ('not_ready', 2), ('cancelled', 130), ('running', 1)]:
            service = AsyncMock()
            service.last_run_outcome = {'state': state}
            service.run_scraper_async.return_value = []
            with self.subTest(state=state), patch.object(api, 'start_default_condition', AsyncMock(return_value=None)), \
                    patch.object(api_server.database, 'run_db', AsyncMock(return_value=[{'id':1}])), \
                    patch('services.ai_coordinator.try_start_job', AsyncMock(return_value=True)), \
                    patch('services.ai_coordinator.finish_job'), \
                    patch('services.scraper_service.ScraperService', return_value=service):
                self.assertEqual(await api_server._run_once_scrape(), expected)
                self.assertTrue(service.track_collection_outcome)


class LegacyOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, outcomes, *, track=True, lock=True):
        from services import scraper_service as service_module
        service = service_module.ScraperService(headless=True)
        service.track_collection_outcome = track
        sites = [dict(id=index, name='시험'+str(index), url='https://example.invalid')
                 for index in range(len(outcomes))]
        engine = AsyncMock()
        engine.scrape_site.side_effect = outcomes
        async def direct(func, *_args, **_kwargs):
            if func is service_module.history_manager.load_history:
                return []
            return {}
        with patch.dict(os.environ, {'SCRAPE_INTERVAL_HOURS':'0'}), \
                patch.object(service_module.utils, 'load_sites', return_value=sites), \
                patch.object(service_module.ai_classifier, 'get_client', return_value=None), \
                patch.object(service_module.database, 'run_db', side_effect=direct), \
                patch.object(service_module, '_acquire_scrape_lock', return_value=object() if lock else None), \
                patch.object(service_module, '_release_scrape_lock'), \
                patch.object(service_module, 'ScraperEngine', return_value=engine):
            result = await service.run_scraper_async()
        return result, service.last_run_outcome, engine

    async def test_genuine_zero_results_are_completed_without_changing_return_type(self):
        result, outcome, engine = await self.collect([{'state':'finished','items':[]}])
        self.assertEqual(result, [])
        self.assertEqual(outcome, {'state':'completed','sites_processed':1,'sites_failed':0})
        self.assertTrue(engine.scrape_site.await_args.kwargs['with_status'])
        engine.close.assert_awaited_once()

    async def test_some_sites_failed_does_not_look_like_successful_zero(self):
        result, outcome, _ = await self.collect([
            {'state':'finished','items':[]}, {'state':'failed','items':[]}])
        self.assertEqual(result, [])
        self.assertEqual(outcome, {'state':'partial_failure','sites_processed':2,'sites_failed':1})

    async def test_partial_page_outcome_remains_a_failure(self):
        result, outcome, _ = await self.collect([{'state':'partial','items':[]}])
        self.assertEqual(result, [])
        self.assertEqual(outcome['state'], 'partial_failure')

    async def test_busy_does_not_start_browser_or_claim_empty_success(self):
        result, outcome, engine = await self.collect([{'state':'finished','items':[]}], lock=False)
        self.assertEqual(result, [])
        self.assertEqual(outcome['state'], 'busy')
        engine.start.assert_not_awaited()

    async def test_normal_gui_does_not_opt_in_to_engine_contract_change(self):
        result, outcome, engine = await self.collect([[]], track=False)
        self.assertEqual(result, [])
        self.assertEqual(outcome['state'], 'completed')
        self.assertNotIn('with_status', engine.scrape_site.await_args.kwargs)


if __name__ == '__main__' and '--lease-probe' in sys.argv:
    # Only this explicitly started synthetic child may exit abnormally.
    path = sys.argv[sys.argv.index('--lease-probe') + 1]
    ownership._lock_path = lambda: path
    child_lease = ownership.acquire()
    if child_lease is None:
        raise SystemExit(3)
    if '--crash-owned-child' in sys.argv:
        os._exit(23)
    if '--hold-until-stdin' in sys.argv:
        # A synthetic sibling owns only its supplied temporary lock path.
        # The parent asks for a normal release; no application process is killed.
        print('LEASE_READY', flush=True)
        sys.stdin.readline()
    child_lease.close()
    raise SystemExit(0)

import unittest
from unittest.mock import Mock, AsyncMock, patch, sentinel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services import news_profiles_api as api, news_workflow_history as history
from services import news_workflow_ownership, policy_sync, app_lifecycle


class TestWorkflowHistoryApi(unittest.TestCase):
    def setUp(self):
        def start(value):
            result=value.start();self.addCleanup(value.stop);return result
        self.clear=start(patch.object(history,'clear_history',return_value={'cleared':2,'news_deleted':False}))
        self.lease=Mock()
        self.acquire=start(patch.object(news_workflow_ownership,'acquire',return_value=self.lease))
        self.lock=start(patch.object(policy_sync,'acquire_db_mutation_lock',new_callable=AsyncMock,return_value=True))
        self.release=start(patch.object(policy_sync,'release_maintenance_lock'))
        start(patch.object(app_lifecycle,'shutdown_requested',False))
        start(patch.dict(api._WORKFLOW_TASKS,{},clear=True))
        async def execute(action):return action(sentinel.connection)
        self.execute=start(patch.object(api,'_owned_execute',side_effect=execute))
        start(patch.dict('os.environ',{'WELFARE_DESKTOP_TOKEN':'history-token'}))
        app=FastAPI();app.add_middleware(app_lifecycle.DesktopAdmissionMiddleware);app.include_router(api.router)
        self.client=TestClient(app);self.client.headers['X-Welfare-Session']='history-token';self.addCleanup(self.client.close)

    def post(self,body=None):
        return self.client.post('/api/news/profiles/p/workflows/clear-history',json=body or {'confirmed':True})

    def test_confirmed_request_scoped_and_releases_locks(self):
        result=self.post({'confirmed':True,'workflow_ids':['one']})
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['news_deleted'],False)
        self.clear.assert_called_once_with(sentinel.connection,'p',workflow_ids=['one'],protected_ids=())
        self.lease.close.assert_called_once();self.release.assert_called_once()

    def test_missing_invalid_confirmation_and_unknown_fields_never_write(self):
        for body in ({'confirmed':False},{'confirmed':'true'},{'confirmed':True,'extra':1},{'confirmed':True,'workflow_ids':[]},{'confirmed':True,'workflow_ids':[1]}):
            with self.subTest(body=body):self.assertIn(self.post(body).status_code,(400,422))
        self.clear.assert_not_called();self.acquire.assert_not_called()

    def test_busy_lease_or_maintenance_never_writes(self):
        self.acquire.return_value=None
        self.assertEqual(self.post().status_code,409);self.clear.assert_not_called()
        self.acquire.return_value=self.lease;self.lock.return_value=False
        self.assertEqual(self.post().status_code,409);self.clear.assert_not_called();self.lease.close.assert_called_once()

    def test_shutdown_is_blocked_and_lock_released(self):
        with patch.object(app_lifecycle,'shutdown_requested',True):self.assertEqual(self.post().status_code,503)
        self.clear.assert_not_called()

    def test_unauthenticated_cannot_clear(self):
        self.client.headers['X-Welfare-Session']='wrong'
        self.assertIn(self.post().status_code,(401,403));self.clear.assert_not_called()

    def test_running_task_ids_are_protected(self):
        task=Mock();task.done.return_value=False
        with patch.dict(api._WORKFLOW_TASKS,{'running-id':task}):self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.clear.call_args.kwargs['protected_ids'],('running-id',))

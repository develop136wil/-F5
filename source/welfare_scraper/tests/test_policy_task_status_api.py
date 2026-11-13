"""Extract only the route AST: no api_server/product profile initialization."""
import ast
import copy
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from services import app_lifecycle


class PolicyTaskStatusApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = ast.parse((Path(__file__).parents[1] / 'api_server.py').read_text(encoding='utf-8'))
        route = copy.deepcopy(next(node for node in source.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'policy_task_status_endpoint'))
        self.app = FastAPI()
        self.app.add_middleware(app_lifecycle.DesktopAdmissionMiddleware)
        exec(compile(ast.Module(body=[route], type_ignores=[]), '<policy-task-route>', 'exec'), {'app': self.app})
        self.statuses = types.SimpleNamespace(**{name: {'running': False, 'checked': 0, 'total': 0, 'message': ''}
            for name in ('ONE_CLICK_STATUS', 'SYNC_STATUS', 'AUDIT_STATUS', 'STYLE_STATUS',
                         'RECOMPUTE_STATUS', 'POLICY_STATUS')})
        fake_services = types.ModuleType('services')
        fake_services.policy_sync = self.statuses
        for patcher in (patch.dict(sys.modules, {'services': fake_services}),
                        patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN': 'status-test-session'}),
                        patch.object(app_lifecycle, 'shutdown_requested', False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_single_read_preserves_six_statuses_priority_and_fields(self):
        self.statuses.ONE_CLICK_STATUS.update(running=True, checked=2, total=3, embedding_failed=1)
        self.statuses.SYNC_STATUS.update(running=True, message='child status')
        before = copy.deepcopy(vars(self.statuses))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.get('/api/policy/task-status', headers={'X-Welfare-Session': 'status-test-session'})
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual([item['label'] for item in items], ['정책 최신화', 'Notion 불러오기', '정책 비교',
            '스타일 제안 생성', '변경 제안 다시 확인', '출처 변경 확인'])
        for item, name in zip(items, before):
            self.assertEqual({key:value for key,value in item.items() if key != 'label'}, before[name])
        self.assertEqual(vars(self.statuses), before, 'Reading must not clear or mutate any status')

    async def test_session_auth_route_matching_and_shutdown_remain_enforced(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            self.assertEqual((await client.get('/api/policy/task-status')).status_code, 403)
            self.assertEqual((await client.get('/api/policy/task-status',
                headers={'X-Welfare-Session': 'wrong'})).status_code, 403)
            headers = {'X-Welfare-Session': 'status-test-session'}
            self.assertEqual((await client.get('/api/policy/task-status', headers=headers)).status_code, 200)
            self.assertEqual((await client.post('/api/policy/task-status', headers=headers)).status_code, 405)
            self.assertEqual((await client.get('/api/policy/task-status-extra', headers=headers)).status_code, 404)
            with patch.object(app_lifecycle, 'shutdown_requested', True):
                self.assertEqual((await client.get('/api/policy/task-status', headers=headers)).status_code, 503)


if __name__ == '__main__':
    unittest.main()

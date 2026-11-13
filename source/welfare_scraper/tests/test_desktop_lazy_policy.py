"""Isolated helper contracts; existing lifecycle suite covers actual shutdown."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).parents[1] / 'api_server.py'

class LazyPolicyStateTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_loaded_policy_busy')
        self.modules = {}
        namespace = {'sys': SimpleNamespace(modules=self.modules)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
        self.check = namespace['_loaded_policy_busy']
        self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(node)))

    def test_absent_policy_stays_unloaded(self):
        self.assertFalse(self.check())
        self.assertEqual(self.modules, {})

    def test_loaded_state_is_sampled_each_time(self):
        state = Mock(return_value='busy')
        self.modules['services.policy_sync'] = SimpleNamespace(maintenance_busy_message=state)
        self.assertTrue(self.check())
        state.return_value = None
        self.assertFalse(self.check())
        self.assertEqual(state.call_count, 2)

    def test_partial_or_unavailable_module_holds_exit(self):
        for policy in (None, SimpleNamespace(), SimpleNamespace(__spec__=SimpleNamespace(_initializing=True))):
            with self.subTest(policy=policy):
                self.modules['services.policy_sync'] = policy
                self.assertTrue(self.check())

    def test_state_failure_holds_exit(self):
        self.modules['services.policy_sync'] = SimpleNamespace(maintenance_busy_message=Mock(side_effect=RuntimeError('unavailable')))
        self.assertTrue(self.check())

    def test_initializing_idle_looking_module_still_holds_exit(self):
        state = Mock(return_value=None)
        self.modules['services.policy_sync'] = SimpleNamespace(__spec__=SimpleNamespace(_initializing=True), maintenance_busy_message=state)
        self.assertTrue(self.check())
        state.assert_not_called()

    def test_desktop_busy_does_not_import_policy_pipeline(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_desktop_busy')
        for n in ast.walk(node):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                self.assertFalse(any('policy_sync' in alias.name for alias in n.names))


class DesktopStatusIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_status_does_not_load_absent_policy_module(self):
        import builtins
        import os
        import sys
        import api_server
        from services import app_lifecycle, ai_coordinator
        original = builtins.__import__
        def forbid_policy(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'services.policy_sync' or (name == 'services' and 'policy_sync' in (fromlist or ())):
                raise AssertionError('status attempted heavy policy import')
            return original(name, globals, locals, fromlist, level)
        with patch.dict(sys.modules), patch.dict(os.environ, WELFARE_DESKTOP_TOKEN='test'), \
             patch.object(app_lifecycle, 'active_requests', 0), \
             patch.object(app_lifecycle, 'active_scheduled_jobs', 0), \
             patch.object(app_lifecycle, 'active_profile_jobs', 0), \
             patch.object(api_server.database, 'has_active_accesses', return_value=False), \
             patch.object(ai_coordinator, 'local_job_status', return_value={'job_kind': None}), \
             patch.dict(api_server.SCRAPER_STATUS, is_running=False), \
             patch.dict(api_server.BATCH_STATUS, is_running=False), \
             patch.dict(api_server.BOKJIRO_STATUS, is_running=False), \
             patch('builtins.__import__', side_effect=forbid_policy):
            sys.modules.pop('services.policy_sync', None)
            result = await api_server.desktop_status()
            self.assertFalse(result['busy'])
            self.assertNotIn('services.policy_sync', sys.modules)

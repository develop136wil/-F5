"""Count-only notifications and extracted wrappers, without app/network startup."""
import ast
import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from services.policy_notifications import policy_result_message


class PolicyNotifications(unittest.TestCase):
    def test_success_reports_proposals_not_automatic_notion_writes(self):
        text = policy_result_message({'ok': True, 'checked': 2, 'changed': 1}, candidates=3)
        self.assertIn('점검 완료*', text)
        self.assertIn('변경 제안 1건', text)
        self.assertIn('신규 후보 3건', text)
        self.assertIn('자동으로 반영하지 않았습니다', text)

    def test_failures_and_holds_are_not_zero_change_success(self):
        for result in ({'ai_failed': 2}, {'review_count': 3},
                       {'ai_failed': 2, 'review_count': 3}, {'ai_preflight_held': 1},
                       {'review_notes': [{'note': 'SECRET SOURCE NEVER SEND'}]}):
            with self.subTest(result=result):
                text = policy_result_message({'ok': True, 'checked': 3, 'changed': 0, **result}, audit=True)
                self.assertIn('최초 감사 일부 완료*', text)
                self.assertIn('앱에서 사유', text)
                self.assertNotIn('SECRET', text)
                if result.get('ai_failed'):
                    self.assertIn('AI 분석 미완료 2건', text)
                if result.get('review_count'):
                    self.assertIn('검토 보류 3건', text)

    def test_pause_and_error_are_not_completion_and_never_expose_raw_errors(self):
        paused = policy_result_message({'ok': True, 'pause_reason': 'source_input_count_unavailable'})
        self.assertIn('잠시 멈춤*', paused)
        for result in (None, {}, {'ok': False, 'error': 'SECRET'},
                       {'ok': 'true', 'checked': False, 'changed': 'SECRET'}):
            text = policy_result_message(result)
            self.assertIn('확인 필요*', text)
            self.assertNotIn('SECRET', text)
            self.assertNotIn('False', text)


class PolicyNotificationWrappers(unittest.IsolatedAsyncioTestCase):
    async def test_both_detail_jobs_send_count_only_partial_summary_and_release_locks(self):
        source = ast.parse((Path(__file__).parents[1] / 'api_server.py').read_text(encoding='utf-8'))
        for name, method, status_key, job in (
            ('_run_policy_check', 'check_policies', 'POLICY_STATUS', 'policy_check'),
            ('_run_policy_audit', 'run_initial_audit', 'AUDIT_STATUS', 'policy_audit'),
        ):
            with self.subTest(name=name):
                node = copy.deepcopy(next(item for item in source.body
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == name))
                result = {'ok': True, 'checked': 2, 'changed': 0, 'ai_failed': 1,
                          'review_count': 1, 'review_notes': [{'note': 'SECRET NEVER SEND'}]}
                policy = types.SimpleNamespace(**{method: AsyncMock(return_value=result),
                    status_key: {}, 'get_new_candidates': Mock(), 'release_maintenance_lock': Mock()})
                coordinator = types.SimpleNamespace(finish_job=Mock())
                notifier = types.SimpleNamespace(send_google_chat_text=AsyncMock())
                namespace = {'List': list, 'database': types.SimpleNamespace(run_db=AsyncMock(return_value=[])),
                             'logger': Mock()}
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<policy-notification-wrapper>', 'exec'), namespace)
                import services
                with patch.object(services, 'policy_sync', policy, create=True), \
                     patch.object(services, 'ai_coordinator', coordinator, create=True), \
                     patch.dict(sys.modules, {'notifier': notifier}):
                    await namespace[name](['fixture'])
                notifier.send_google_chat_text.assert_awaited_once()
                text = notifier.send_google_chat_text.await_args.args[0]
                self.assertIn('일부 완료*', text)
                self.assertIn('AI 분석 미완료 1건', text)
                self.assertIn('검토 보류 1건', text)
                self.assertNotIn('SECRET', text)
                coordinator.finish_job.assert_called_once_with(job)
                policy.release_maintenance_lock.assert_called_once()


if __name__ == '__main__':
    unittest.main()

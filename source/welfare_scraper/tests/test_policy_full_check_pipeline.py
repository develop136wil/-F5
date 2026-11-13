"""Synthetic unified-workflow tests; no provider or operating profile access."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import database
from services import policy_full_check as flow, policy_sync as ps
from tests.test_check_policies_baseline_failure import TestCheckPoliciesBaselineExtractionFailure as Fixture


class Pipeline(unittest.IsolatedAsyncioTestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown

    def mocks(self, stack, *, check=None, style=None):
        actions = {}
        for name, value in {
            'sync_policies': {'ok': True, 'synced': 1},
            'recompute_diffs': {'ok': True},
            'check_policies': check or {'ok': True, 'initial_compared': 1, 'changed': 1},
            'restyle_pending_diffs': style or {'ok': True, 'styled': 1},
            'run_initial_audit': {'ok': True},
        }.items():
            actions[name] = stack.enter_context(patch.object(ps, name, new=AsyncMock(return_value=value)))
        actions['preview'] = stack.enter_context(patch.object(ps, 'preview_style_work',
            return_value={'configured': True, 'eligible_count': 1}))
        actions['embedding'] = stack.enter_context(patch.object(flow, 'fill_missing_embeddings',
            new=AsyncMock(return_value={'completed': 1, 'failed': 0, 'pause_reason': ''})))
        return actions

    async def test_main_uses_conditional_stages_no_separate_duplicate_initial_audit(self):
        with ExitStack() as stack:
            actions = self.mocks(stack)
            result = await ps.run_full_check(['medical'])
        actions['sync_policies'].assert_awaited_once_with(['medical'], defer_embeddings=True)
        actions['recompute_diffs'].assert_awaited_once_with(['medical'], only_needed=True)
        actions['check_policies'].assert_awaited_once_with(['medical'], include_initial=True, stop_on_quota=True)
        actions['restyle_pending_diffs'].assert_awaited_once_with(['medical'], only_needed=True)
        actions['run_initial_audit'].assert_not_awaited()
        self.assertEqual(ps.ONE_CLICK_STATUS['stages'], ['sync', 'recompute', 'check', 'style', 'embedding'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(ps.ONE_CLICK_STATUS['initial_compared'], 1)
        self.assertFalse(ps.ONE_CLICK_STATUS['running'])

    async def test_quota_pause_skips_optional_work_and_preserves_finished_counts(self):
        with ExitStack() as stack:
            actions = self.mocks(stack, check={'ok': True, 'initial_compared': 1,
                'changed': 1, 'pause_reason': 'local_budget_exhausted'})
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(result['initial_compared'], 1)
        actions['restyle_pending_diffs'].assert_not_awaited()
        actions['embedding'].assert_not_awaited()
        self.assertIn('보존', ps.ONE_CLICK_STATUS['message'])

    async def test_unconfigured_style_never_spends_style_request(self):
        with ExitStack() as stack:
            actions = self.mocks(stack)
            actions['preview'].return_value = {'configured': False, 'eligible_count': 0}
            await ps.run_full_check(['medical'])
        actions['restyle_pending_diffs'].assert_not_awaited()
        actions['embedding'].assert_awaited_once()

    async def test_uncertain_legacy_receipt_is_not_reported_all_complete(self):
        with ExitStack() as stack:
            actions = self.mocks(stack)
            actions['sync_policies'].return_value = {'ok': True, 'synced': 1,
                                                     'verification_pending': 1}
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['verification_pending'], 1)
        self.assertIn('이전 반영 결과 확인 필요 1건', ps.ONE_CLICK_STATUS['message'])

    async def test_validated_but_rejected_styling_is_partial_not_all_success(self):
        with ExitStack() as stack:
            self.mocks(stack, style={'ok': True, 'styled': 0, 'rejected_fields': 1})
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['style_failed'], 0)
        self.assertEqual(result['style_rejected_fields'], 1)
        self.assertIn('문장 정리 검증 보류 1개 항목', ps.ONE_CLICK_STATUS['message'])
        self.assertIn('변경 제안의 원문과 표현', ps.ONE_CLICK_STATUS['message'])
        self.assertNotIn('확인이 필요한 정책', ps.ONE_CLICK_STATUS['message'])

    async def test_stage_failure_counts_are_separate_and_cleared_on_next_run(self):
        with ExitStack() as stack:
            actions = self.mocks(stack, check={'ok': True, 'initial_compared': 1,
                'changed': 0, 'ai_failed': 1},
                style={'ok': True, 'styled': 1, 'failed': 2, 'rejected_fields': 3})
            actions['embedding'].return_value = {'completed': 4, 'failed': 5, 'pause_reason': ''}
            result = await ps.run_full_check(['medical'])
        for key, count in {'style_failed': 2, 'style_rejected_fields': 3,
                           'embedding_completed': 4, 'embedding_failed': 5}.items():
            self.assertEqual(result[key], count)
            self.assertEqual(ps.ONE_CLICK_STATUS[key], count)
        message = ps.ONE_CLICK_STATUS['message']
        for text in ('AI 분석 미완료 1건', '문장 정리 실패 2건',
                     '문장 정리 검증 보류 3개 항목', '제목 검색 데이터 준비 실패 5건',
                     '정책 정보 최신인지 확인하기', '재시도', '완료한 비교와 제안은 보존'):
            self.assertIn(text, message)
        self.assertNotIn('확인이 필요한 정책', message)
        with ExitStack() as stack:
            self.mocks(stack)
            recovered = await ps.run_full_check(['medical'])
        self.assertEqual(recovered['state'], 'completed')
        for key in ('ai_failed', 'style_failed', 'style_rejected_fields', 'embedding_failed'):
            self.assertEqual(ps.ONE_CLICK_STATUS[key], 0)

    async def test_review_panel_guidance_requires_actual_notes_not_just_count(self):
        for notes in ([], [{'title': '검토 정책', 'code': 'source_read_incomplete', 'note': '원문 확인 필요'}]):
            with self.subTest(notes=bool(notes)), ExitStack() as stack:
                self.mocks(stack, check={'ok': True, 'review_count': 1, 'review_notes': notes})
                result = await ps.run_full_check(['medical'])
            self.assertEqual(result['state'], 'partial')
            self.assertEqual('확인이 필요한 정책' in ps.ONE_CLICK_STATUS['message'], bool(notes))
            self.assertNotIn('검토 안내가 있는 항목', ps.ONE_CLICK_STATUS['message'])

    async def test_quota_pause_keeps_real_review_guidance_and_separate_counts(self):
        with ExitStack() as stack:
            actions = self.mocks(stack, check={'ok': True, 'initial_compared': 1,
                'pause_reason': 'daily_limit', 'review_count': 1,
                'review_notes': [{'title': '검토 정책', 'code': 'source_missing', 'note': '원문 없음'}]})
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(result['embedding_completed'], 0)
        self.assertEqual(result['embedding_failed'], 0)
        self.assertIn('확인이 필요한 정책', ps.ONE_CLICK_STATUS['message'])
        self.assertIn('AI 연결·사용 한도', ps.ONE_CLICK_STATUS['message'])
        actions['embedding'].assert_not_awaited()

    async def test_sync_failure_stops_and_clears_all_running_flags(self):
        with ExitStack() as stack:
            actions = self.mocks(stack)
            actions['sync_policies'].return_value = {'ok': False, 'error': 'fixture'}
            for child in (ps.SYNC_STATUS, ps.POLICY_STATUS, ps.AUDIT_STATUS, ps.RECOMPUTE_STATUS, ps.STYLE_STATUS):
                child['running'] = True
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'error')
        actions['check_policies'].assert_not_awaited()
        self.assertTrue(all(not child['running'] for child in
            (ps.ONE_CLICK_STATUS, ps.SYNC_STATUS, ps.POLICY_STATUS, ps.AUDIT_STATUS, ps.RECOMPUTE_STATUS, ps.STYLE_STATUS)))

    async def test_child_exception_preserves_error_state_and_unlocks_progress(self):
        with ExitStack() as stack:
            actions = self.mocks(stack)
            actions['check_policies'].side_effect = RuntimeError('synthetic')
            with self.assertRaises(RuntimeError):
                await ps.run_full_check(['medical'])
        self.assertEqual(ps.ONE_CLICK_STATUS['state'], 'error')
        self.assertFalse(ps.ONE_CLICK_STATUS['running'])
        actions['embedding'].assert_not_awaited()

    async def test_check_failure_is_not_overwritten_as_completed(self):
        with ExitStack() as stack:
            actions = self.mocks(stack, check={'ok': False, 'error': 'synthetic'})
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['state'], 'error')
        self.assertFalse(result['ok'])
        self.assertFalse(ps.ONE_CLICK_STATUS['running'])
        actions['restyle_pending_diffs'].assert_not_awaited()
        actions['embedding'].assert_not_awaited()

    def test_plan_is_local_estimate_not_google_quota_claim(self):
        with patch.object(ps, 'preview_style_work', return_value={'configured': False, 'eligible_count': 0}), \
             patch.object(ps, '_automatic_recompute_ids', return_value={1}), \
             patch.object(flow, 'public_ai_budget', return_value={'remaining': None, 'reason': ''}):
            result = flow.get_plan(['medical'])
        self.assertEqual(result['policy_count'], 1)
        self.assertEqual(result['initial_count'], 1)
        self.assertEqual(result['pending_count'], 1)
        self.assertIsNone(result['ai_remaining'])
        self.assertIn('대상 수와 AI 요청 수는 다릅니다', result['estimate_note'])
        self.assertNotIn('앱의 설정 한도', result['estimate_note'])

    async def test_title_embedding_deferred_during_sync_preserves_mirror(self):
        page = {'page_id': 'page-baseline-test', 'title': '테스트 정책',
                '_source_urls': ['https://test.example/policy'], 'support_detail': '지원 10만원'}
        with patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock()) as ai:
            await ps._embed_and_upsert_batch('medical', [page], defer_embeddings=True)
        ai.assert_not_awaited()
        row = database.execute_query('SELECT notion_fields,title_embedding FROM notion_policies', fetch_one=True)
        self.assertEqual(json.loads(row['notion_fields'])['support_detail'], '지원 10만원')
        self.assertIsNone(row['title_embedding'])

    async def test_title_backfill_respects_remaining_units_and_only_updates_embedding(self):
        before = database.execute_query('SELECT notion_fields,source_hashes FROM notion_policies', fetch_one=True)
        with patch.object(flow, 'ai_pause_reason', return_value=''), \
             patch.object(ps.embedding_service, 'get_unavailable_reason', return_value=None), \
             patch('services.ai_coordinator.input_request_budget', return_value=1), \
             patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock(return_value=[[1.0, 0.0]])) as ai:
            result = await flow.fill_missing_embeddings(['medical'])
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(ai.await_args.args[0]), 1)
        self.assertEqual(dict(before), dict(database.execute_query(
            'SELECT notion_fields,source_hashes FROM notion_policies', fetch_one=True)))
        with patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock()) as again:
            await flow.fill_missing_embeddings(['medical'])
        again.assert_not_awaited()

    async def test_title_backfill_pause_is_not_an_attempt_or_deletion(self):
        with patch.object(flow, 'ai_pause_reason', return_value='daily_limit'), \
             patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock()) as ai:
            result = await flow.fill_missing_embeddings(['medical'])
        ai.assert_not_awaited()
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM notion_policies', fetch_one=True)['n'], 1)

    async def test_title_backfill_respects_independent_embedding_daily_hold(self):
        with patch.object(flow, 'ai_pause_reason', return_value=''), \
             patch.object(ps.embedding_service, 'get_unavailable_reason', return_value='daily_quota_exhausted'), \
             patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock()) as ai:
            result = await flow.fill_missing_embeddings(['medical'])
        ai.assert_not_awaited()
        self.assertEqual(result['pause_reason'], 'daily_limit')

    async def test_title_backfill_new_daily_hold_pauses_after_first_failed_batch(self):
        with patch.object(flow, 'ai_pause_reason', return_value=''), \
             patch.object(ps.embedding_service, 'get_unavailable_reason', side_effect=[None, 'daily_quota_exhausted']), \
             patch('services.ai_coordinator.input_request_budget', return_value=1), \
             patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock(return_value=[None])) as ai:
            result = await flow.fill_missing_embeddings(['medical'])
        ai.assert_awaited_once()
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertIsNone(database.execute_query('SELECT title_embedding FROM notion_policies', fetch_one=True)['title_embedding'])

    def test_replaced_source_without_pending_proposal_requires_first_comparison_again(self):
        database.execute_write("UPDATE notion_policies SET audited_at='done'")
        ps._upsert_policy('medical', {'page_id': 'page-baseline-test', 'title': '테스트 정책',
                                     '_source_urls': ['https://example.invalid/replaced']}, None)
        self.assertIsNone(database.execute_query('SELECT audited_at FROM notion_policies', fetch_one=True)['audited_at'])

    async def test_current_comparison_logic_does_not_recompute_or_lose_style_marker(self):
        pending = {'logic_version': ps._DIFF_LOGIC_VERSION, 'fields': [], 'custom': 'preserve'}
        database.execute_write("UPDATE notion_policies SET change_status='detected',last_extracted_fields=?,pending_diff=?",
                               ('{"support_detail":"지원 10만원"}', json.dumps(pending)))
        with patch.object(ps, 'get_changes', return_value=[{'id': 1, 'apply_ready': True}]), \
             patch.object(ps, '_diff_fields') as compare:
            result = await ps.recompute_diffs(['medical'], only_needed=True)
        compare.assert_not_called()
        self.assertEqual(result['checked'], 0)
        self.assertEqual(json.loads(database.execute_query('SELECT pending_diff FROM notion_policies', fetch_one=True)['pending_diff']), pending)


if __name__ == '__main__':
    unittest.main()

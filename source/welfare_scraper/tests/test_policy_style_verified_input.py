"""Whole-request style safety with a fake tokenizer, no provider generation."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as quota, ai_verified_input as verified, policy_sync as ps
from tests import test_policy_auto_style as fixtures


class WholeStyleInput(unittest.IsolatedAsyncioTestCase):
    add_policy = fixtures.AutomaticPolicyStyle.add_policy
    proposal = fixtures.AutomaticPolicyStyle.proposal
    replace_proposal = fixtures.AutomaticPolicyStyle.replace_proposal

    def setUp(self):
        fixtures.AutomaticPolicyStyle.setUp(self)
        verified._reset_for_tests()
        self.addCleanup(verified._reset_for_tests)
        self.count = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
        self.client.return_value = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=self.count)))
        self.stack.enter_context(patch.object(quota, 'input_token_capacity', return_value=6000))

    async def test_complete_old_new_and_guide_tail_survive_in_exact_verified_request(self):
        old = '기존 공개 정책 설명입니다.\n' * 100 + '이전 마지막 안내'
        new = '공개 정책 내용입니다.\n' * 200 + '새 마지막 안내'
        self.guide = '간결하게 정리합니다.\n' * 40 + '가이드 끝'
        self.add_policy(old=old, new=new)
        self.ai.return_value = {self.label: new}
        await ps.restyle_pending_diffs(['test'], only_needed=True)
        self.ai.assert_awaited_once()
        prompt = self.ai.await_args.args[1]
        for text in (old, new, self.guide):
            self.assertIn(text, prompt)
        self.count.assert_awaited_once()
        kwargs = self.ai.await_args.kwargs
        receipt = kwargs['verified_input']
        self.assertTrue(receipt.is_provider_counted)
        self.assertLess(verified.input_tokens_for(receipt, model=kwargs['model_override'], prompt=prompt), 6000)

    async def test_private_tail_never_reaches_tokenizer_or_generation(self):
        self.add_policy(new='공개 정책 안내입니다.\n' * 200 + '담당자 휴대전화: 010-1234-5678')
        result = await ps.restyle_pending_diffs(['test'], only_needed=True)
        self.assertEqual(result['failed'], 1)
        self.ai.assert_not_awaited()
        self.count.assert_not_awaited()
        self.assertNotIn('styled', self.proposal()['fields'][0])

    async def test_count_unavailable_pauses_without_fingerprinting_input_as_processed(self):
        self.add_policy(new='공개 정책 안내입니다.\n' * 200)
        before = self.rows[1]['pending_diff']
        self.count.side_effect = OSError('fake offline')
        result = await ps.restyle_pending_diffs(['test'], only_needed=True)
        self.assertTrue(result['paused'])
        self.assertEqual(result['pause_reason'], 'source_input_count_unavailable')
        self.assertEqual(self.rows[1]['pending_diff'], before)
        self.ai.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_true_oversize_keeps_original_and_is_not_a_temporary_count_failure(self):
        self.add_policy(new='공개 정책 안내입니다.\n' * 200)
        original = self.proposal()['fields'][0]['new']
        self.count.return_value = SimpleNamespace(total_tokens=9000)
        result = await ps.restyle_pending_diffs(['test'], only_needed=True)
        self.assertFalse(result['paused'])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.proposal()['fields'][0]['new'], original)
        self.ai.assert_not_awaited()

    async def test_temporary_reservation_hold_preserves_retryable_proposal(self):
        self.add_policy()
        before = self.rows[1]['pending_diff']
        self.ai.side_effect = quota.AiTemporaryRateLimit(20)
        result = await ps.restyle_pending_diffs(['test'], only_needed=True)
        self.assertTrue(result['paused'])
        self.assertEqual(result['pause_reason'], 'source_input_rate_limited')
        self.assertEqual(self.rows[1]['pending_diff'], before)
        self.write.assert_not_awaited()

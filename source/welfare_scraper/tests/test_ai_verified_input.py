"""Request-bound tokenizer receipts; entirely synthetic, no provider traffic."""
import asyncio
import dataclasses
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as coordinator, ai_rate_policy as rate
from services import ai_verified_input as verified
from services.ai_input_safety import AiInputReviewRequired


MODEL = rate.MODELS[0]
PROMPT = '지원 신청 안내입니다. 신청 대상과 기간을 모두 확인합니다.\n' * 180
SCHEMA = {'type': 'object', 'properties': {'대상': {'type': 'string'}}, 'required': ['대상']}


class TestVerifiedInput(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verified._reset_for_tests()
        self.addCleanup(verified._reset_for_tests)
        pace = patch.object(verified, '_COUNT_INTERVAL', 0)
        pace.start()
        self.addCleanup(pace.stop)
        self.count = AsyncMock(return_value=SimpleNamespace(total_tokens=1500))
        self.generate = AsyncMock()
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=self.count, generate_content=self.generate)))

    async def check(self, prompt=PROMPT, schema=SCHEMA, model=MODEL, budget=6000):
        return await verified.verify_input(self.client, prompt, schema,
            model=model, input_token_budget=budget)

    async def test_large_korean_request_retains_all_text_and_schema_allowance(self):
        receipt = await self.check()
        self.count.assert_awaited_once()
        self.assertEqual(self.count.await_args.kwargs['contents'], PROMPT)
        self.assertEqual(self.count.await_args.kwargs['model'], MODEL)
        config = self.count.await_args.kwargs['config']
        self.assertEqual(config.http_options.retry_options.attempts, 1)
        self.assertEqual(config.http_options.timeout, 12000)
        schema_bytes = len(json.dumps(SCHEMA, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        self.assertEqual(receipt.input_tokens, 1500 + 256 + math.ceil(1500 * .1) + schema_bytes + 128)
        self.assertTrue(receipt.is_provider_counted)
        self.assertGreater(coordinator.estimate_generation_input_tokens(PROMPT, SCHEMA), 6000)
        self.assertLess(receipt.input_tokens, 6000)
        self.generate.assert_not_awaited()

    async def test_short_requests_stay_local_and_quota_is_not_changed(self):
        receipt = await self.check(prompt='지원 신청 안내')
        self.assertEqual(receipt.method, 'local_bytes')
        self.assertFalse(receipt.is_provider_counted)
        self.assertEqual(receipt.input_tokens, coordinator.estimate_generation_input_tokens('지원 신청 안내', SCHEMA))
        self.count.assert_not_awaited()

    async def test_exact_request_cache_expires_and_never_contains_plain_text(self):
        first = await self.check()
        second = await self.check()
        self.assertEqual(first.input_tokens, second.input_tokens)
        self.count.assert_awaited_once()
        self.assertNotIn('지원 신청', repr(verified._CACHE))
        self.assertNotIn('지원 신청', repr(first))
        with patch.object(verified, '_CACHE_TTL', -1):
            await self.check()
        self.assertEqual(self.count.await_count, 2)

    async def test_immutable_receipts_reject_prompt_schema_model_substitution(self):
        receipt = await self.check()
        self.assertEqual(verified.input_tokens_for(receipt, model=MODEL, prompt=PROMPT,
            response_json_schema=SCHEMA), receipt.input_tokens)
        for changes in ({'prompt': PROMPT + '다른 내용'}, {'model': 'different-model'},
                        {'response_json_schema': {'type': 'array'}}):
            args = dict(model=MODEL, prompt=PROMPT, response_json_schema=SCHEMA)
            args.update(changes)
            with self.subTest(changes=changes.keys()), self.assertRaises(ValueError):
                verified.input_tokens_for(receipt, **args)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.input_tokens = 1
        with self.assertRaises(ValueError):
            verified.input_tokens_for(SimpleNamespace(input_tokens=1), model=MODEL, prompt=PROMPT)
        with self.assertRaises(ValueError):
            verified.input_tokens_for(dataclasses.replace(receipt, input_tokens=1),
                model=MODEL, prompt=PROMPT, response_json_schema=SCHEMA)

    async def test_sensitive_prompt_tail_and_schema_never_reach_counter(self):
        for prompt, schema in ((PROMPT + '\n010-1234-5678', SCHEMA),
                               (PROMPT, {'description': '010-1234-5678'})):
            with self.subTest(schema=schema), self.assertRaises(AiInputReviewRequired):
                await self.check(prompt=prompt, schema=schema)
        self.count.assert_not_awaited()

    async def test_count_failure_keeps_conservative_estimate_without_leaking_exception(self):
        self.count.side_effect = RuntimeError('SENSITIVE_PROVIDER_ERROR')
        with self.assertLogs(verified.logger, level='INFO') as captured:
            receipt = await self.check()
        self.assertEqual(receipt.method, 'fallback_count_unavailable')
        self.assertFalse(receipt.is_provider_counted)
        self.assertEqual(receipt.input_tokens, coordinator.estimate_generation_input_tokens(PROMPT, SCHEMA))
        self.assertNotIn('SENSITIVE_PROVIDER_ERROR', ' '.join(captured.output))
        cooldown = await self.check(prompt=PROMPT + '추가')
        self.assertEqual(cooldown.method, 'fallback_count_cooldown')
        self.count.assert_awaited_once()

    async def test_invalid_count_never_means_zero_or_verified(self):
        for count in (None, True, 0, -1, 1.5, '100'):
            verified._reset_for_tests()
            self.count.return_value = SimpleNamespace(total_tokens=count)
            result = await self.check()
            self.assertEqual(result.method, 'fallback_count_invalid')
            self.assertGreater(result.input_tokens, 6000)

    async def test_count_timeout_is_bounded_and_cancellation_propagates(self):
        async def pending(**kwargs):
            await asyncio.sleep(10)
        self.count.side_effect = pending
        with patch.object(verified, '_COUNT_TIMEOUT', .01):
            result = await self.check()
        self.assertEqual(result.method, 'fallback_count_timeout')
        self.assertFalse(verified._COUNT_ACTIVE)
        verified._reset_for_tests()
        self.count.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.check()
        self.assertFalse(verified._COUNT_ACTIVE)
        self.assertEqual(verified._BLOCKED_UNTIL, 0)

    async def test_count_dispatch_guard_exception_is_not_a_provider_fallback(self):
        guard = AsyncMock(side_effect=ValueError('stale synthetic claim'))
        with self.assertRaisesRegex(ValueError, 'stale synthetic claim'):
            await verified.verify_input(self.client, PROMPT, SCHEMA, model=MODEL,
                input_token_budget=6000, before_dispatch=guard)
        guard.assert_awaited_once()
        self.count.assert_not_awaited()
        self.assertFalse(verified._COUNT_ACTIVE)
        self.assertEqual(verified._BLOCKED_UNTIL, 0)
        self.assertFalse(verified._CACHE)

    async def test_count_dispatch_guard_runs_after_pacing_and_cancel_sends_nothing(self):
        events = []
        async def slot():
            events.append('slot')
            return True
        async def guard():
            events.append('guard')
            raise asyncio.CancelledError()
        with patch.object(verified, '_take_count_slot', side_effect=slot):
            with self.assertRaises(asyncio.CancelledError):
                await verified.verify_input(self.client, PROMPT, SCHEMA, model=MODEL,
                    input_token_budget=6000, before_dispatch=guard)
        self.assertEqual(events, ['slot', 'guard'])
        self.count.assert_not_awaited()
        self.assertFalse(verified._COUNT_ACTIVE)

    async def test_current_invalid_budget_and_oversize_count_payload_send_nothing(self):
        for budget in (0, -1, True, '6000'):
            result = await self.check(budget=budget)
            self.assertEqual(result.method, 'fallback_invalid_budget')
        with patch.object(verified, '_MAX_COUNT_BYTES', 5):
            result = await self.check()
            self.assertEqual(result.method, 'fallback_request_too_large')
        self.count.assert_not_awaited()

    async def test_provider_count_can_still_be_too_large(self):
        self.count.return_value = SimpleNamespace(total_tokens=10000)
        result = await self.check()
        self.assertTrue(result.is_provider_counted)
        self.assertGreater(result.input_tokens, 6000)

    async def test_concurrent_identical_requests_share_one_counter_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(total_tokens=1500)
        self.count.side_effect = delayed
        first = asyncio.create_task(self.check())
        await entered.wait()
        second = asyncio.create_task(self.check())
        await asyncio.sleep(0)
        self.assertEqual(self.count.await_count, 1)
        release.set()
        one, two = await asyncio.gather(first, second)
        self.assertEqual(one.input_tokens, two.input_tokens)
        self.count.assert_awaited_once()
        self.assertFalse(verified._COUNT_ACTIVE)

    async def test_count_call_pacing_and_window_are_bounded(self):
        clock = [100.0]
        async def advance(seconds):
            clock[0] += seconds
        with patch.object(verified.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(verified.asyncio, 'sleep', side_effect=advance), \
             patch.object(verified, '_COUNT_INTERVAL', 2), \
             patch.object(verified, '_COUNT_PER_MINUTE', 2):
            self.assertTrue(await verified._take_count_slot())
            verified._release_count_slot()
            self.assertTrue(await verified._take_count_slot())
            self.assertEqual(clock[0], 102)
            verified._release_count_slot()
            self.assertTrue(await verified._take_count_slot())
            self.assertEqual(clock[0], 160)
            verified._release_count_slot()


class TestVerifiedGenerationAdmission(unittest.IsolatedAsyncioTestCase):
    check = TestVerifiedInput.check

    def setUp(self):
        TestVerifiedInput.setUp(self)
        temp = tempfile.TemporaryDirectory(prefix='verified-input-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        ledger = patch.object(coordinator, '_state_path', return_value=str(self.root / 'state.json'))
        ledger.start()
        self.addCleanup(ledger.stop)
        env = patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '20'})
        env.start()
        self.addCleanup(env.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)

    async def test_verified_request_reserves_current_shared_allowance(self):
        import ai_classifier
        receipt = await self.check()
        with patch.object(ai_classifier, 'complete_response_text', return_value='{"ok": true}'):
            result = await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1,
                verified_input=receipt)
        self.assertTrue(result['ok'])
        self.generate.assert_awaited_once()
        ledger = json.loads((self.root / 'state.json.minute.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['entries'][0][2], receipt.input_tokens)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)
        self.assertEqual(coordinator.input_token_budget(MODEL), 6000)

    async def test_old_call_still_rejects_same_request_before_provider(self):
        import ai_classifier
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1)
        self.generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_valid_receipt_cannot_override_lower_current_budget(self):
        import ai_classifier
        receipt = await self.check()
        key = 'synthetic-key-for-test'
        observation = rate.observe({MODEL: {'rpm': 10, 'tpm': 1000, 'rpd': 20},
            rate.MODELS[1]: {'rpm': 10, 'tpm': 10000, 'rpd': 20}}, key=key)
        (self.root / 'config.json').write_text(json.dumps({'GOOGLE_API_KEY': key,
            rate.POLICY_KEY: observation}), encoding='utf-8')
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1,
                verified_input=receipt)
        self.generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_mismatched_receipt_never_reserves_or_generates(self):
        import ai_classifier
        receipt = await self.check()
        with self.assertRaises(ValueError):
            await ai_classifier._call_gemini_json(self.client, PROMPT + '다른 내용', '합성 요청',
                model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1,
                verified_input=receipt)
        self.generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_generation_dispatch_guard_runs_after_quota_wait_and_blocks_sdk(self):
        import ai_classifier
        receipt = await self.check()
        events = []
        async def admission(*args, **kwargs):
            events.append('admitted')
        async def guard():
            events.append('guard')
            raise asyncio.CancelledError()
        with patch.object(coordinator, 'acquire_model_slot', side_effect=admission):
            with self.assertRaises(asyncio.CancelledError):
                await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                    model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1,
                    verified_input=receipt, before_dispatch=guard)
        self.assertEqual(events, ['admitted', 'guard'])
        self.generate.assert_not_awaited()

    async def test_generation_guard_keeps_conservative_reservation_and_propagates(self):
        import ai_classifier
        receipt = await self.check()
        guard = AsyncMock(side_effect=ValueError('stale synthetic claim'))
        with self.assertRaisesRegex(ValueError, 'stale synthetic claim'):
            await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                model_override=MODEL, response_json_schema=SCHEMA, request_attempts=1,
                verified_input=receipt, before_dispatch=guard)
        self.generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)

    async def test_generation_guard_errors_never_become_provider_retries(self):
        import ai_classifier
        receipt = await self.check()
        guard = AsyncMock(side_effect=RuntimeError('429 synthetic local guard failure'))
        with patch.object(coordinator, 'register_api_error') as register:
            with self.assertRaisesRegex(RuntimeError, 'synthetic local guard'):
                await ai_classifier._call_gemini_json(self.client, PROMPT, '합성 요청',
                    model_override=MODEL, response_json_schema=SCHEMA, request_attempts=3,
                    verified_input=receipt, before_dispatch=guard)
        register.assert_not_called()
        guard.assert_awaited_once()
        self.generate.assert_not_awaited()

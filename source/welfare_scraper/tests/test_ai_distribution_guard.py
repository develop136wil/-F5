"""No app daily cap; durable usage/provider/preflight guards, synthetic I/O."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as coordinator
from services import ai_rate_policy as rate
from services.ai_input_safety import AiInputReviewRequired

GEN, EMBED = rate.MODELS
INPUT_TOKENS = coordinator.estimate_input_tokens('합성 정책 안내')


class TestDistributionBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='distribution-budget-')
        self.addCleanup(self.tmp.cleanup)
        state = str(Path(self.tmp.name) / 'quota.json')
        self.config = Path(self.tmp.name) / 'config.json'
        # Supported models, enough observed minute capacity for the daily-budget
        # race. No provider request or operational configuration is involved.
        key = 'synthetic-distribution-budget-key-not-a-credential'
        self.config.write_text(json.dumps({'GOOGLE_API_KEY': key,
            rate.POLICY_KEY: rate.observe({
                GEN: {'rpm': 100, 'tpm': 100000, 'rpd': 1000},
                EMBED: {'rpm': 100, 'tpm': 100000, 'rpd': 1000}}, key=key)}), encoding='utf-8')
        for context in (patch.object(coordinator, '_state_path', return_value=state),
                        patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '2'})):
            context.start()
            self.addCleanup(context.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)

    async def test_usage_includes_models_and_survives_restart_without_local_cap(self):
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=INPUT_TOKENS)
        coordinator._reset_for_tests()
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        status = coordinator.status()
        self.assertEqual(status['recorded_requests_today'], 3)
        self.assertFalse(status['local_budget_exhausted'])
        self.assertIsNone(status['local_budget_remaining'])
        self.assertIsNone(status['daily_request_limit'])

    async def test_saved_legacy_zero_and_stale_environment_do_not_block(self):
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.config.write_text(json.dumps({'AI_DAILY_REQUEST_LIMIT': '0'}), encoding='utf-8')
        await coordinator.acquire_model_slot(EMBED, input_tokens=INPUT_TOKENS)
        self.assertIsNone(coordinator.status()['daily_request_limit'])
        self.config.write_text(json.dumps({'AI_DAILY_REQUEST_LIMIT': '3'}), encoding='utf-8')
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=INPUT_TOKENS)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 3)

    def test_damaged_observation_uses_minute_safeguards_not_a_daily_cap(self):
        self.config.write_text('{broken', encoding='utf-8')
        Path(str(self.config) + '.bak').write_text(json.dumps({'AI_DAILY_REQUEST_LIMIT': '20'}), encoding='utf-8')
        self.assertIsNone(coordinator.daily_request_limit())
        coordinator.reserve_sync_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(coordinator.input_token_capacity(GEN), 6000)
        self.assertEqual(self.config.read_text(encoding='utf-8'), '{broken')

    async def test_legacy_zero_does_not_create_provider_hold(self):
        with patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '0'}):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(coordinator._DAILY_EXHAUSTED, {})
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 2)

    async def test_pacific_rollover_restores_provider_hold_without_erasing_same_day(self):
        with patch.object(coordinator, '_pacific_today', return_value='2026-09-09'):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=INPUT_TOKENS)
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=INPUT_TOKENS)
            info = coordinator.register_api_error(GEN, RuntimeError('429 RequestsPerDay RESOURCE_EXHAUSTED'))
            self.assertTrue(info['daily_exhausted'])
            coordinator._reset_for_tests()
            with self.assertRaises(coordinator.AiDailyQuotaExhausted):
                coordinator.reserve_sync_model_slot(GEN, input_tokens=INPUT_TOKENS)
            self.assertEqual(coordinator.status()['recorded_requests_today'], 2)
        with patch.object(coordinator, '_pacific_today', return_value='2026-09-10'):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            self.assertEqual(coordinator.status()['recorded_requests_today'], 1)

    async def test_concurrent_models_preserve_all_counts_without_legacy_cap(self):
        with patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '5'}):
            outcomes = await asyncio.gather(*[
                coordinator.acquire_model_slot((GEN, EMBED)[index % 2], input_tokens=INPUT_TOKENS)
                for index in range(8)], return_exceptions=True)
        self.assertEqual(sum(not isinstance(result, Exception) for result in outcomes), 8)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 8)

    def test_deprecated_settings_cannot_reenable_a_daily_cap(self):
        for raw in ('-1', '1001', 'twenty', '1.5', '', '２０', '0', '9' * 5000):
            with self.subTest(raw=raw), patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': raw}):
                self.assertIsNone(coordinator.daily_request_limit())
                coordinator.reserve_sync_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 8)

    async def test_provider_not_called_or_counted_for_private_prompt(self):
        import ai_classifier
        generate = AsyncMock()
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with self.assertRaises(AiInputReviewRequired):
            await ai_classifier._call_gemini_json(client, '연락처 010-2345-6789', '합성 입력')
        generate.assert_not_called()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_legacy_zero_does_not_block_a_public_prompt(self):
        import ai_classifier
        generate = AsyncMock(return_value=SimpleNamespace(text='{}'))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '0'}):
            await ai_classifier._call_gemini_json(client, '교육비 월 5만원 지원', '합성 입력')
        generate.assert_awaited_once()
        self.assertEqual(coordinator._DAILY_EXHAUSTED, {})

    async def test_site_analysis_preflight_and_provider_hold_do_not_send(self):
        import site_analyzer
        generate = AsyncMock()
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with patch.object(site_analyzer, 'get_genai_client', return_value=client), \
             patch.object(site_analyzer._site_rate_limiter, 'acquire', new_callable=AsyncMock):
            private = await site_analyzer.analyze_with_ai('<p>개인 연락처 010-2345-6789</p>', 'https://example.invalid')
            self.assertEqual(private['code'], 'AI_INPUT_REVIEW_REQUIRED')
            self.assertEqual(coordinator.status()['recorded_requests_today'], 0)
            coordinator.mark_daily_exhausted(GEN)
            paused = await site_analyzer.analyze_with_ai('<p>기관 게시판</p>', 'https://example.invalid')
            self.assertFalse(paused['success'])
            self.assertIn('일일', paused['error'])
            self.assertEqual(coordinator.status()['recorded_requests_today'], 0)
        generate.assert_not_called()

    def test_preflight_and_budget_are_not_automatically_retried(self):
        from services.news_retry_policy import decide_retry
        for error, reason in ((AiInputReviewRequired(), 'ai_input_review_required'),
                              (coordinator.AiLocalBudgetExhausted(), 'local_budget_exhausted')):
            decision = decide_retry(error, attempts=1)
            self.assertTrue(decision.terminal)
            self.assertEqual(decision.reason, reason)


class TestBudgetConfig(unittest.IsolatedAsyncioTestCase):
    async def test_config_exposes_no_app_limit_or_api_key(self):
        import api_server
        with patch.object(api_server.utils, 'get_env_value', return_value=''), \
             patch.object(coordinator, 'daily_request_limit', return_value=None):
            result = await api_server.get_config()
        self.assertIsNone(result['AI_DAILY_REQUEST_LIMIT'])
        self.assertEqual(result['GOOGLE_API_KEY'], '')

    async def test_config_ignores_deprecated_values_before_writing(self):
        import api_server
        for raw in ('-1', '1001', '1.5', '２０', '', '9' * 5000):
            with self.subTest(raw=raw), patch.object(api_server.utils, 'set_env_values') as save:
                await api_server.save_config(api_server.ConfigRequest(items={'AI_DAILY_REQUEST_LIMIT': raw}))
                save.assert_not_called()

    async def test_config_ignores_legacy_budget_without_changing_keys(self):
        import api_server
        with patch.object(api_server.utils, 'set_env_values') as save:
            await api_server.save_config(api_server.ConfigRequest(items={'AI_DAILY_REQUEST_LIMIT': ' 25 '}))
        save.assert_not_called()

    async def test_legacy_budget_save_is_ignored_without_reservation_lock(self):
        import api_server
        with patch.object(coordinator, 'request_budget_config_lock',
                          side_effect=coordinator.AiQuotaStateUnavailable()), \
             patch.object(api_server.utils, 'set_env_values') as save:
            await api_server.save_config(api_server.ConfigRequest(items={'AI_DAILY_REQUEST_LIMIT': '0'}))
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ai_coordinator
from services import ai_rate_policy as rate

GEN, EMBED = rate.MODELS
INPUT_TOKENS = ai_coordinator.estimate_input_tokens('합성 정책 안내')


class TestClientRequestBudget(unittest.TestCase):
    def test_sdk_retries_are_explicitly_disabled(self):
        from google import genai
        with patch.object(genai, 'Client') as client, patch.dict(os.environ, {'AI_REQUEST_TIMEOUT_SEC': '45'}):
            ai_coordinator.create_client('synthetic-key')
        options = client.call_args.kwargs['http_options']
        self.assertEqual(options.timeout, 45000)
        self.assertEqual(options.retry_options.attempts, 1)


class TestAiJobGate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(
            ai_coordinator, "_state_path", return_value=os.path.join(self.tmp.name, "state.json")
        )
        self.state_patch.start()
        self.lock_patch = patch.object(
            ai_coordinator, "_job_lock_path", return_value=os.path.join(self.tmp.name, "ai_job.lock")
        )
        self.lock_patch.start()
        ai_coordinator._reset_for_tests()

    def tearDown(self):
        ai_coordinator._reset_for_tests()
        self.state_patch.stop()
        self.lock_patch.stop()
        self.tmp.cleanup()

    async def test_only_one_heavy_ai_job_can_run(self):
        self.assertTrue(await ai_coordinator.try_start_job("scrape", "정보 수집"))
        self.assertFalse(await ai_coordinator.try_start_job("policy", "정책 점검"))
        self.assertIn("정보 수집", ai_coordinator.busy_message())
        self.assertEqual(ai_coordinator.status()["job_kind"], "scrape")

    async def test_matching_owner_releases_job(self):
        await ai_coordinator.try_start_job("scrape", "정보 수집")
        ai_coordinator.finish_job("policy")
        self.assertTrue(ai_coordinator.status()["busy"])
        ai_coordinator.finish_job("scrape")
        self.assertFalse(ai_coordinator.status()["busy"])
        self.assertTrue(await ai_coordinator.try_start_job("policy", "정책 점검"))

    async def test_external_process_lock_refuses_job_and_reports_busy(self):
        external = ai_coordinator._acquire_process_lock()
        self.assertIsNotNone(external)
        try:
            self.assertFalse(await ai_coordinator.try_start_job("scrape", "정보 수집"))
            self.assertTrue(ai_coordinator.status()["busy"])
            self.assertIn("다른 프로그램", ai_coordinator.busy_message())
        finally:
            ai_coordinator._release_process_lock(external)
        self.assertFalse(ai_coordinator.status()["busy"])
        self.assertTrue(await ai_coordinator.try_start_job("scrape", "정보 수집"))


class TestModelRateQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Test the short process-local queue, not a real 60-second common wait.
        # This synthetic observation keeps the durable common protections active.
        key = 'synthetic-rate-queue-key-not-a-credential'
        with open(os.path.join(self.tmp.name, 'config.json'), 'w', encoding='utf-8') as handle:
            json.dump({'GOOGLE_API_KEY': key, rate.POLICY_KEY: rate.observe({
                GEN: {'rpm': 100, 'tpm': 100000, 'rpd': 1000},
                EMBED: {'rpm': 100, 'tpm': 100000, 'rpd': 1000}}, key=key)}, handle)
        self.state_patch = patch.object(
            ai_coordinator, "_state_path", return_value=os.path.join(self.tmp.name, "state.json")
        )
        self.state_patch.start()
        self.lock_patch = patch.object(
            ai_coordinator, "_job_lock_path", return_value=os.path.join(self.tmp.name, "ai_job.lock")
        )
        self.lock_patch.start()
        ai_coordinator._reset_for_tests()

    def tearDown(self):
        ai_coordinator._reset_for_tests()
        self.state_patch.stop()
        self.lock_patch.stop()
        self.tmp.cleanup()

    async def test_same_model_calls_share_one_rate_queue(self):
        with patch.dict(os.environ, {
            "AI_GLOBAL_GENERATE_RPM": "2",
            "AI_GLOBAL_RATE_PERIOD_SEC": "0.5",
        }), patch.object(ai_coordinator, '_save_state'):
            # Queue behavior only: disk fsync latency must not consume the tiny
            # test window. Persistence has a separate restart regression below.
            results = await asyncio.gather(*[
                ai_coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS) for _ in range(3)
            ])
        self.assertEqual(results.count(True), 1)
        self.assertGreaterEqual(len(ai_coordinator._MODEL_CALLS[GEN]), 1)
        result = ai_coordinator.status()
        self.assertEqual(result['recorded_requests_today'], 3)
        self.assertEqual(result['usage_date'], ai_coordinator._pacific_today())
        self.assertIn(result['usage_timezone'], ('America/Los_Angeles', 'UTC'))

    async def test_request_count_survives_restart_without_quota_exhaustion(self):
        with patch.object(ai_coordinator, '_pacific_today', return_value='2026-09-07'):
            await ai_coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            ai_coordinator._reset_for_tests()
            self.assertEqual(ai_coordinator.status()['recorded_requests_today'], 1)
            await ai_coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            ai_coordinator._reset_for_tests()
            self.assertEqual(ai_coordinator.status()['recorded_requests_today'], 2)
        with patch.object(ai_coordinator, '_pacific_today', return_value='2026-09-08'):
            self.assertEqual(ai_coordinator.status()['recorded_requests_today'], 0)

    async def test_429_cooldown_delays_next_caller(self):
        with patch.dict(os.environ, {"AI_GLOBAL_GENERATE_RPM": "10"}):
            ai_coordinator.set_model_cooldown(GEN, 0.04)
            started = time.monotonic()
            waited = await ai_coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertTrue(waited)
        self.assertGreaterEqual(time.monotonic() - started, 0.03)

    async def test_daily_exhaustion_survives_reload_and_blocks_calls(self):
        ai_coordinator.mark_daily_exhausted(GEN)
        ai_coordinator._reset_for_tests()
        with self.assertRaises(ai_coordinator.AiDailyQuotaExhausted):
            await ai_coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertTrue(ai_coordinator.status()["models"][GEN]["daily_exhausted"])

    def test_error_classification_and_registration(self):
        error = RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '12s'")
        info = ai_coordinator.register_api_error(GEN, error)
        self.assertTrue(info["rate_limited"])
        self.assertEqual(info["retry_after"], 12.0)
        self.assertGreater(ai_coordinator.status()["models"][GEN]["cooldown_seconds"], 0)

    async def test_api_timeout_is_bounded(self):
        async def slow():
            await asyncio.sleep(1)
        with self.assertRaises(ai_coordinator.AiRequestTimeout):
            await ai_coordinator.await_api(slow(), timeout_seconds=0.01)


if __name__ == "__main__":
    unittest.main()

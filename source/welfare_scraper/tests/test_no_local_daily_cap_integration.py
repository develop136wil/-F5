"""Legacy profiles do not impose a daily cap; real provider holds still apply.

Actual embedding entry points and coordinator admission, fake SDK only. All
credentials, usage ledgers and configurations live in temporary directories.
A virtual clock advances a minute per dispatch so this is not a burst test.
"""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import ai_coordinator, ai_rate_policy
from tests.test_embedding_input_safety import load_embedding_service, response


class NoLocalDailyCapIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_twenty_dispatches_survive_old_caps_and_policy_expiry(self):
        for policy_state in ('unconfirmed', 'current', 'expired'):
            for old_cap in (0, 20):
                with self.subTest(policy_state=policy_state, old_cap=old_cap), tempfile.TemporaryDirectory(prefix='no-daily-cap-') as folder:
                    root = Path(folder)
                    state = root / 'quota.json'
                    key = 'synthetic-not-a-real-api-key'
                    config = {'GOOGLE_API_KEY': key, 'AI_DAILY_REQUEST_LIMIT': old_cap}
                    if policy_state != 'unconfirmed':
                        config[ai_rate_policy.POLICY_KEY] = ai_rate_policy.observe({
                            model: {'rpm': 60, 'tpm': 1000000, 'rpd': 1}
                            for model in ai_rate_policy.MODELS}, key=key)
                        if policy_state == 'expired':
                            config[ai_rate_policy.POLICY_KEY]['observed_on'] = (
                                ai_rate_policy.today() - datetime.timedelta(days=8)).isoformat()
                    config_path = root / 'config.json'
                    config_path.write_text(json.dumps(config), encoding='utf-8')
                    original_config = config_path.read_bytes()

                    spec = importlib.util.spec_from_file_location('synthetic_uncapped_coordinator', ai_coordinator.__file__)
                    coordinator = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(coordinator)
                    coordinator._state_path = lambda: str(state)
                    clock = [coordinator.time.time()]
                    fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0], sleep=lambda seconds: None)
                    coordinator.time = fake_time
                    service = load_embedding_service()
                    service.ai_coordinator = coordinator
                    provider = Mock(return_value=response())
                    client = SimpleNamespace(models=SimpleNamespace(embed_content=provider))

                    with patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': str(old_cap)}), patch.object(ai_rate_policy, 'time', fake_time):
                        for number in range(25):
                            text = f'공공기관 안내 {number}'
                            if number % 2:
                                result = service.embed_text(text, client=client)
                            else:
                                result = await service.embed_text_async(text, client=client)
                            self.assertEqual(result, [1.0, 0.0])
                            clock[0] += 61
                        status = coordinator.status()
                        self.assertEqual(provider.call_count, 25)
                        self.assertEqual(status['recorded_requests_today'], 25)
                        self.assertIsNone(status['daily_request_limit'])
                        self.assertIsNone(status['local_budget_remaining'])
                        self.assertFalse(status['local_budget_exhausted'])
                        self.assertIsNone(coordinator.daily_request_limit())

                        # A real provider signal still prevents the next request,
                        # persists across process restart, and does not add usage.
                        info = coordinator.register_api_error(service.EMBED_MODEL,
                            RuntimeError('429 RESOURCE_EXHAUSTED REQUESTS PER DAY'))
                        self.assertTrue(info['daily_exhausted'])
                        coordinator._reset_for_tests()
                        self.assertIsNone(await service.embed_text_async('중단 이후', client=client))
                        self.assertIsNone(service.embed_text('중단 이후 동기', client=client))
                        self.assertEqual(provider.call_count, 25)
                        self.assertEqual(coordinator.status()['recorded_requests_today'], 25)
                        self.assertTrue(coordinator.status()['models'][service.EMBED_MODEL]['daily_exhausted'])

                    self.assertEqual(config_path.read_bytes(), original_config)
                    saved = json.loads(state.read_text(encoding='utf-8'))
                    self.assertEqual(sum(saved['calls'].values()), 25)
                    self.assertNotIn(key, state.read_text(encoding='utf-8'))
                    self.assertNotIn('공공기관 안내', state.read_text(encoding='utf-8'))

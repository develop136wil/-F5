"""Quota observations are local, explicit, strict and do not reset usage."""
import json
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
import api_server as api
from services import ai_coordinator as coordinator, ai_rate_policy as policy


class QuotaObservationApiTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'GOOGLE_API_KEY': 'test-public-placeholder',
            'AI_DAILY_REQUEST_LIMIT': '200', 'unrelated': 'preserve'}), encoding='utf-8')
        for ctx in (patch.object(api.utils, 'get_app_data_dir', return_value=str(self.root)),
                    patch.object(coordinator, '_state_path', return_value=str(self.root / 'quota.json'))):
            ctx.start()
            self.addCleanup(ctx.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)
        self.models = {policy.MODELS[0]: {'rpm': 15, 'tpm': 250000, 'rpd': 500},
                       policy.MODELS[1]: {'rpm': 100, 'tpm': 30000, 'rpd': 1000}}

    async def test_explicit_save_preserves_usage_key_and_other_settings(self):
        coordinator.reserve_sync_model_slot(policy.MODELS[0], input_tokens=100)
        with patch.object(api.utils, 'set_env_values') as save:
            result = await api.save_quota_observation(api.QuotaObservationRequest(
                confirmed=True, models=self.models))
        saved = json.loads(save.call_args.args[0][policy.POLICY_KEY])
        self.assertEqual(set(save.call_args.args[0]), {policy.POLICY_KEY})
        self.assertEqual(saved['observed_on'], policy.today().isoformat())
        self.assertEqual(result['quota_policy']['effective'][policy.MODELS[0]],
                         {'rpm': 10, 'tpm': 150000, 'rpd': None})
        self.assertEqual(result['quota_policy']['observed'][policy.MODELS[0]]['rpd'], 500)
        self.assertEqual(saved['models'][policy.MODELS[1]]['rpd'], 1000)
        self.assertNotIn('key_binding', result['quota_policy'])
        self.assertNotIn('test-public-placeholder', json.dumps(result))
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)
        self.assertIsNone(coordinator.daily_request_limit())

    async def test_false_confirmation_and_unsafe_values_do_not_write(self):
        with patch.object(api.utils, 'set_env_values') as save:
            with self.assertRaises(HTTPException):
                await api.save_quota_observation(api.QuotaObservationRequest(
                    confirmed=False, models=self.models))
            for invalid in (True, '15', 15.0, -1, 10001):
                self.models[policy.MODELS[0]]['rpm'] = invalid
                with self.subTest(invalid=invalid), self.assertRaises(HTTPException):
                    await api.save_quota_observation(api.QuotaObservationRequest(
                        confirmed=True, models=self.models))
            save.assert_not_called()

    async def test_missing_key_and_bad_config_fail_closed(self):
        for content in ('{}', '{broken', '[]'):
            self.config.write_text(content, encoding='utf-8')
            with patch.object(api.utils, 'set_env_values') as save:
                with self.assertRaises(HTTPException):
                    await api.save_quota_observation(api.QuotaObservationRequest(
                        confirmed=True, models=self.models))
                save.assert_not_called()

    def test_confirmation_requires_boolean(self):
        for value in ('true', 1, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                api.QuotaObservationRequest(confirmed=value, models=self.models)

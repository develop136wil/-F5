"""User-supplied dated project observation, not a universal quota default."""
import datetime
import unittest
from services import ai_rate_policy as rate


class ObservedProjectLimits(unittest.TestCase):
    def test_pdf_minute_limits_have_margin_but_daily_limit_is_observation_only(self):
        values = {rate.MODELS[0]: dict(rpm=15, tpm=250000, rpd=500),
                  rate.MODELS[1]: dict(rpm=100, tpm=30000, rpd=1000)}
        observation = rate.observe(values, key='synthetic-observation-key',
                                   current_date=datetime.date(2026, 9, 18))
        current = dict(state='current', **observation)
        self.assertEqual(rate.effective_limits(current, rate.MODELS[0]),
                         dict(rpm=10, tpm=150000, rpd=None))
        self.assertEqual(rate.effective_limits(current, rate.MODELS[1]),
                         dict(rpm=60, tpm=18000, rpd=None))
        for model in rate.MODELS:
            for field in ('rpm', 'tpm'):
                self.assertLess(rate.effective_limits(current, model)[field], values[model][field])
            self.assertEqual(observation['models'][model]['rpd'], values[model]['rpd'])
            self.assertIsNone(rate.effective_limits(current, model)['rpd'])
        # Screenshots must not make every new user's unobserved account inherit them.
        self.assertIsNone(rate.effective_limits({'state': 'unconfirmed'}, rate.MODELS[0]))
        with self.assertRaises(ValueError):
            rate.parse_observation(observation, key='different-key', current_date=datetime.date(2026, 9, 18))
        with self.assertRaises(ValueError):
            rate.parse_observation(observation, key='synthetic-observation-key', current_date=datetime.date(2026, 9, 25))

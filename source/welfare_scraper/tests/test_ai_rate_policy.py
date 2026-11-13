"""Dated quota policy and shared minute admission; synthetic inputs, API zero.

Each coordinator owns only this test's temporary ledger/config. No SDK clients,
credentials, operational DB, Notion, or provider requests are used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from services import ai_coordinator as coordinator
from services import ai_rate_policy as rate


DAY = datetime.date(2026, 9, 9)
NOW = 1000.0
GEN, EMBED = rate.MODELS
KEY = 'synthetic-quota-test-key-not-a-credential'


def observation():
    # Deliberately small synthetic limits; these are not provider quota claims.
    return rate.observe({GEN: {'rpm': 3, 'tpm': 1000, 'rpd': 100},
                         EMBED: {'rpm': 4, 'tpm': 2000, 'rpd': 200}},
                        key=KEY, current_date=DAY)


def peer_for(path, name):
    spec = importlib.util.spec_from_file_location(name, Path(coordinator.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._state_path = lambda: str(path)
    module._pacific_today = lambda: DAY.isoformat()
    return module


class TestObservationPolicy(unittest.TestCase):
    def test_observation_is_current_for_seven_calendar_days_only(self):
        for offset in (0, 1, 6):
            with self.subTest(offset=offset):
                parsed = rate.parse_observation(observation(), key=KEY,
                    current_date=DAY + datetime.timedelta(days=offset))
                self.assertEqual(parsed['observed_on'], DAY.isoformat())
        for offset in (-1, 7, 30):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                rate.parse_observation(observation(), key=KEY,
                    current_date=DAY + datetime.timedelta(days=offset))

    def test_key_binding_does_not_expose_key_and_does_not_transfer(self):
        value = observation()
        self.assertNotIn(KEY, json.dumps(value))
        self.assertEqual(rate.key_binding(' ' + KEY + '\n'), value['key_binding'])
        for key in ('', 'another-synthetic-key'):
            with self.subTest(key=bool(key)), self.assertRaises(ValueError):
                rate.parse_observation(value, key=key, current_date=DAY)

    def test_supported_models_require_independent_explicit_limits(self):
        for change in ('missing', 'extra'):
            value = observation()
            if change == 'missing':
                value['models'].pop(EMBED)
            else:
                value['models']['unobserved-model'] = dict(value['models'][GEN])
            with self.subTest(change=change), self.assertRaises(ValueError):
                rate.parse_observation(value, key=KEY, current_date=DAY)
        current = dict(state='current', **observation())
        self.assertEqual(rate.effective_limits(current, 'unobserved-model'),
                         {'rpm': 0, 'tpm': 0, 'rpd': None})
        self.assertNotEqual(rate.effective_limits(current, GEN), rate.effective_limits(current, EMBED))

    def test_numeric_limits_are_strict_integers_and_bounded(self):
        for field, maximum in (('rpm', 10000), ('tpm', 10000000), ('rpd', 1000000)):
            for bad in (True, False, '15', 15.0, -1, None, maximum + 1):
                value = observation()
                value['models'][GEN][field] = bad
                with self.subTest(field=field, bad=repr(bad)), self.assertRaises(ValueError):
                    rate.parse_observation(value, key=KEY, current_date=DAY)

    def test_policy_version_does_not_accept_boolean_or_float(self):
        for bad in (True, 1.0, '1', 0, 2):
            value = observation()
            value['version'] = bad
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                rate.parse_observation(value, key=KEY, current_date=DAY)

    def test_zero_minute_observation_is_pause_but_rpd_is_metadata_only(self):
        value = observation()
        for model in rate.MODELS:
            value['models'][model] = {'rpm': 0, 'tpm': 0, 'rpd': 0}
        self.assertEqual(rate.effective_limits(dict(state='current', **value), GEN),
                         {'rpm': 0, 'tpm': 0, 'rpd': None})
        value['models'][GEN] = {'rpm': 1, 'tpm': 1, 'rpd': 1}
        self.assertEqual(rate.effective_limits(dict(state='current', **value), GEN),
                         {'rpm': 1, 'tpm': 1, 'rpd': None})

    def test_public_status_omits_key_binding(self):
        result = rate.public_status(dict(state='current', **observation()))
        self.assertNotIn('key_binding', json.dumps(result))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertTrue(result['input_tokens_are_estimates'])
        self.assertEqual(result['effective'][GEN], {'rpm': 2, 'tpm': 600, 'rpd': None})
        self.assertEqual(result['observed'][GEN]['rpd'], 100)

    def test_estimate_counts_utf8_media_and_batch_overhead_without_exact_claim(self):
        self.assertEqual(rate.estimate_input_tokens('a'), 385)
        self.assertEqual(rate.estimate_input_tokens('한'), 387)
        self.assertEqual(rate.estimate_input_tokens(['a', '한']), 516)
        self.assertEqual(rate.estimate_input_tokens('a', media_bytes=1025), 897)
        for bad in (None, {}, ['a', 1], b'a'):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                rate.estimate_input_tokens(bad)
        for bad in (True, -1, 1.5, '1'):
            with self.subTest(media=repr(bad)), self.assertRaises(ValueError):
                rate.estimate_input_tokens('a', media_bytes=bad)


class TestMinuteWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ai-minute-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'minute.json'

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def test_window_prunes_expired_entries_and_retains_other_model(self):
        self.write({'version': 1, 'entries': [[GEN, NOW-60, 2], [GEN, NOW-59, 3],
                    [EMBED, NOW-1, 4]], 'penalties': {}})
        result = rate.read_window(self.path, now=NOW)
        self.assertEqual(result['entries'], [[GEN, NOW-59, 3], [EMBED, NOW-1, 4]])

    def test_missing_window_is_empty_but_malformed_window_is_not_reset(self):
        self.assertEqual(rate.read_window(self.path, now=NOW)['entries'], [])
        for value in ({}, [], {'version': 1, 'entries': 'x', 'penalties': {}},
                      {'version': 1, 'entries': [], 'penalties': []}):
            self.write(value)
            before = self.path.read_bytes()
            with self.subTest(value=value), self.assertRaises(ValueError):
                rate.read_window(self.path, now=NOW)
            self.assertEqual(self.path.read_bytes(), before)

    def test_window_version_is_strict_integer(self):
        for bad in (True, 1.0, '1'):
            self.write({'version': bad, 'entries': [], 'penalties': {}})
            with self.subTest(version=repr(bad)), self.assertRaises(ValueError):
                rate.read_window(self.path, now=NOW)

    def test_window_rejects_bad_numeric_rows_even_if_expired(self):
        for timestamp, tokens in ((True, 1), (NOW+2, 1), (float('nan'), 1),
                                  (float('inf'), 1), (NOW, True), (NOW, -1),
                                  (NOW-100, 1.2), (NOW, '1')):
            self.write({'version': 1, 'entries': [[GEN, timestamp, tokens]], 'penalties': {}})
            with self.subTest(timestamp=timestamp, tokens=tokens), self.assertRaises(ValueError):
                rate.read_window(self.path, now=NOW)

    def test_shared_rpm_and_tpm_wait_use_only_requested_model(self):
        limits = {'rpm': 2, 'tpm': 1000, 'rpd': 10}
        window = {'entries': [[GEN, NOW-50, 700], [GEN, NOW-10, 200],
                               [EMBED, NOW, 50000]], 'penalties': {}}
        result = rate.window_admission(window, GEN, 400, limits, now=NOW)
        self.assertFalse(result['too_large'])
        self.assertAlmostEqual(result['wait'], 10.05)
        result = rate.window_admission(window, GEN, 900, limits, now=NOW)
        self.assertAlmostEqual(result['wait'], 50.05)

    def test_oversized_single_input_is_not_an_endless_wait(self):
        limits = {'rpm': 2, 'tpm': 1000, 'rpd': 10}
        window = {'entries': [], 'penalties': {}}
        self.assertEqual(rate.window_admission(window, GEN, 1001, limits, now=NOW),
                         {'too_large': True, 'wait': 0})
        for bad in (None, True, '10', 1.1, -1):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                rate.window_admission(window, GEN, bad, limits, now=NOW)

    def test_429_halves_limits_with_floor_and_never_raises_previous_penalty(self):
        window = {'version': 1, 'entries': [], 'penalties': {}}
        for expected in (.5, .25, .125, .125):
            rate.lower_after_rate_error(window, GEN, 10, now=NOW)
            self.assertEqual(window['penalties'][GEN]['factor'], expected)
        limits = {'rpm': 8, 'tpm': 8000, 'rpd': 10}
        delayed = rate.window_admission(window, GEN, 1001, limits, now=NOW)
        self.assertFalse(delayed['too_large'])
        self.assertTrue(delayed['temporary_capacity'])
        self.assertGreaterEqual(delayed['wait'], 3600)
        self.assertEqual(rate.window_admission(window, GEN, 900, limits, now=NOW)['wait'], 10)
        self.write(window)
        self.assertEqual(rate.read_window(self.path, now=NOW+3601)['penalties'], {})

    def test_malformed_penalty_fails_closed(self):
        for factor in (True, 0, 2, float('nan')):
            self.write({'version': 1, 'entries': [], 'penalties': {
                GEN: {'until': NOW+3600, 'cooldown_until': NOW+30, 'factor': factor}}})
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                rate.read_window(self.path, now=NOW)

    def test_long_provider_cooldown_survives_normal_penalty_expiry(self):
        window = {'version': 1, 'entries': [], 'penalties': {}}
        rate.lower_after_rate_error(window, GEN, 7200, now=NOW, limit_kind='requests')
        self.write(window)
        restored = rate.read_window(self.path, now=NOW + 3601)
        result = rate.window_admission(restored, GEN, 100,
            {'rpm': 2, 'tpm': 1000, 'rpd': 10}, now=NOW + 3601)
        self.assertEqual(result['wait'], 3599)


class TestSharedDatedAdmission(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ai-dated-admission-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state.json'
        self.config = self.root / 'config.json'
        self.window = self.root / 'state.json.minute.json'
        self.data = {'GOOGLE_API_KEY': KEY, rate.POLICY_KEY: observation(), 'AI_DAILY_REQUEST_LIMIT': 1000}
        self.save_config()
        for patcher in (patch.object(coordinator, '_state_path', return_value=str(self.state)),
                        patch.object(coordinator, '_pacific_today', return_value=DAY.isoformat()),
                        patch.object(rate, 'today', return_value=DAY),
                        patch.object(time, 'time', return_value=NOW)):
            patcher.start()
            self.addCleanup(patcher.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)

    def save_config(self):
        self.config.write_text(json.dumps(self.data), encoding='utf-8')

    def daily(self):
        return json.loads(self.state.read_text(encoding='utf-8'))

    def minute(self):
        return rate.read_window(self.window, now=NOW)

    def test_sync_reservation_records_both_ledgers_once(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        self.assertEqual(self.minute()['entries'], [[GEN, NOW, 100]])

    async def test_async_reservation_records_both_ledgers_once(self):
        waited = await coordinator.acquire_model_slot(GEN, input_tokens=100)
        self.assertFalse(waited)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        self.assertEqual(len(self.minute()['entries']), 1)

    def test_current_observation_requires_estimate_and_refuses_oversized_before_count(self):
        for estimate in (None, 601, True, '100', -1):
            with self.subTest(estimate=estimate), self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(GEN, input_tokens=estimate)
            self.assertEqual(self.daily()['calls'], {})
            self.assertEqual(rate.read_window(self.window, now=NOW)['entries'], [])

    def test_separate_instance_observes_shared_rpm_and_other_model_still_works(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        peer = peer_for(self.state, 'dated_rate_peer')
        peer.reserve_sync_model_slot(GEN, input_tokens=100)
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        peer.reserve_sync_model_slot(EMBED, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 2, EMBED: 1})
        self.assertEqual(len(self.minute()['entries']), 3)

    def test_peer_cannot_bypass_shared_tpm_with_small_rpm_count(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
        peer = peer_for(self.state, 'dated_tpm_peer')
        with self.assertRaises(peer._RateWindowWait):
            peer.reserve_sync_model_slot(GEN, input_tokens=101)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        self.assertEqual(len(self.minute()['entries']), 1)

    def test_usage_does_not_reset_and_old_daily_budgets_do_not_block(self):
        self.data[rate.POLICY_KEY]['models'][GEN]['rpd'] = 2
        self.data['AI_DAILY_REQUEST_LIMIT'] = 2
        self.save_config()
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=100)
        self.data[rate.POLICY_KEY]['models'][GEN]['rpd'] = 100
        self.save_config()
        coordinator._reset_for_tests()
        # The two current-minute requests are still counted across restart.
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        with patch.object(time, 'time', return_value=NOW + 61):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 3, EMBED: 1})

    def test_unknown_model_and_provider_hold_block_but_observed_zero_rpd_does_not(self):
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot('unknown-model', input_tokens=100)
        self.data[rate.POLICY_KEY]['models'][GEN]['rpd'] = 0
        self.save_config()
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        coordinator.mark_daily_exhausted(EMBED)
        coordinator._reset_for_tests()
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=100)

    def test_malformed_minute_file_preserves_bytes_and_does_not_dispatch(self):
        self.window.write_text('{invalid-synthetic-minute', encoding='utf-8')
        before = self.window.read_bytes()
        with self.assertRaises(coordinator.AiQuotaStateUnavailable):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.window.read_bytes(), before)
        self.assertEqual(self.daily()['calls'], {})

    def test_daily_save_failure_after_minute_write_only_overcounts_not_admits(self):
        coordinator._load_state()
        with patch.object(coordinator, '_save_state', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {})
        self.assertEqual(coordinator._DAILY_CALLS.get(GEN, 0), 0)
        self.assertEqual(self.minute()['entries'], [[GEN, NOW, 100]])

    def test_minute_save_failure_never_increments_daily(self):
        coordinator._load_state()
        original = coordinator._atomic_json
        def fail_minute(path, value):
            if str(path).endswith('.minute.json'):
                raise OSError('synthetic minute write failure')
            return original(path, value)
        with patch.object(coordinator, '_atomic_json', side_effect=fail_minute):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {})
        self.assertFalse(self.window.exists())

    def test_confirmed_429_reduction_is_shared_and_does_not_consume_daily(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        info = coordinator.register_api_error(GEN, RuntimeError("429 retryDelay: '12s'"))
        self.assertTrue(info['rate_limited'])
        self.assertEqual(self.minute()['penalties'][GEN]['factor'], .5)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        peer = peer_for(self.state, 'dated_429_peer')
        with self.assertRaises(peer._RateWindowWait):
            peer.reserve_sync_model_slot(GEN, input_tokens=100)
        peer.reserve_sync_model_slot(EMBED, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 1, EMBED: 1})

    def test_failed_shared_429_write_raises_durable_daily_hold(self):
        original = coordinator._atomic_json
        def fail_minute(path, value):
            if str(path).endswith('.minute.json'):
                raise OSError('synthetic minute write failure')
            return original(path, value)
        with patch.object(coordinator, '_atomic_json', side_effect=fail_minute):
            coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        coordinator._reset_for_tests()
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['exhausted'], {GEN: DAY.isoformat()})

    def test_expired_or_key_changed_observation_does_not_drop_saved_429_hold(self):
        coordinator.register_api_error(GEN, RuntimeError("429 retryDelay: '30s'"))
        original = copy.deepcopy(self.data)
        for reason in ('expired', 'key_changed'):
            self.data = copy.deepcopy(original)
            if reason == 'expired':
                self.data[rate.POLICY_KEY]['observed_on'] = (DAY-datetime.timedelta(days=7)).isoformat()
            else:
                self.data['GOOGLE_API_KEY'] = 'new-synthetic-key'
            self.save_config()
            coordinator._reset_for_tests()
            with self.subTest(reason=reason), self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(GEN, input_tokens=100)

    def test_expiry_never_raises_a_previously_smaller_same_key_observation(self):
        self.data[rate.POLICY_KEY]['models'][GEN] = {'rpm': 1, 'tpm': 500, 'rpd': 1}
        previous = rate.effective_limits(dict(state='current', **self.data[rate.POLICY_KEY]), GEN)
        self.data[rate.POLICY_KEY]['observed_on'] = (DAY-datetime.timedelta(days=7)).isoformat()
        self.save_config()
        after = rate.effective_limits(coordinator._quota_policy(), GEN)
        self.assertIsNotNone(after)
        for field in ('rpm', 'tpm'):
            with self.subTest(field=field):
                self.assertLessEqual(after[field], previous[field])
        self.assertIsNone(after['rpd'])

    def test_expiry_never_turns_a_known_zero_observation_into_permission(self):
        self.data[rate.POLICY_KEY]['models'][GEN] = {'rpm': 0, 'tpm': 0, 'rpd': 0}
        self.data[rate.POLICY_KEY]['observed_on'] = (DAY-datetime.timedelta(days=7)).isoformat()
        self.save_config()
        with self.assertRaises((coordinator.AiDailyQuotaExhausted, coordinator.AiQuotaStateUnavailable)):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {})

    def test_removing_observation_does_not_clear_recorded_rate_hold(self):
        coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        coordinator._reset_for_tests()
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)

    def test_new_429_during_reconfirmation_is_recorded_for_other_instances(self):
        self.data[rate.POLICY_KEY]['observed_on'] = (DAY-datetime.timedelta(days=7)).isoformat()
        self.save_config()
        coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        self.assertIn(GEN, self.minute()['penalties'])
        peer = peer_for(self.state, 'dated_new_429_expired_peer')
        with self.assertRaises(peer._RateWindowWait):
            peer.reserve_sync_model_slot(GEN, input_tokens=100)

    def test_new_429_after_policy_removal_keeps_shared_existing_minute_protection(self):
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        self.window.write_text(json.dumps({'version': 1, 'entries': [], 'penalties': {}}), encoding='utf-8')
        coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        self.assertIn(GEN, self.minute()['penalties'])
        peer = peer_for(self.state, 'dated_new_429_unconfirmed_peer')
        with self.assertRaises(peer._RateWindowWait):
            peer.reserve_sync_model_slot(GEN, input_tokens=100)

    def test_normal_minute_wait_is_not_published_as_broken_state_warning(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=101)
        self.assertIsNone(coordinator._STATE_WARNING)

    def test_numeric_provider_status_without_status_in_text_is_recognized(self):
        for field, code, expected in (('code', 429, 'rate_limited'),
                                      ('status_code', 429, 'rate_limited'),
                                      ('code', 503, 'unavailable')):
            error = RuntimeError('Synthetic provider error without a numeric message')
            setattr(error, field, code)
            with self.subTest(field=field, code=code):
                self.assertTrue(coordinator.analyze_api_error(error)[expected])

    def test_provider_daily_metrics_are_recognized_for_generation_and_embedding(self):
        for metric in ('GenerateRequestsPerDayPerProjectPerModel-FreeTier',
                       'GenerateRequestsPerDayPerProjectPerModel',
                       'EmbedRequestsPerDayPerProjectPerModel-FreeTier',
                       'RequestsPerDay', 'requests per day', 'RPD quota exhausted'):
            with self.subTest(metric=metric):
                info = coordinator.analyze_api_error(RuntimeError('429 ' + metric))
                self.assertTrue(info['daily_exhausted'])
                self.assertFalse(info['local_budget_exhausted'])
        for metric in ('RequestsPerMinute', 'InputTokensPerMinute', 'RESOURCE_EXHAUSTED'):
            with self.subTest(metric=metric):
                self.assertFalse(coordinator.analyze_api_error(
                    RuntimeError('429 ' + metric))['daily_exhausted'])

    def test_mention_of_daily_quota_without_provider_rate_failure_is_not_exhaustion(self):
        for message in ('RequestsPerDay observation is unavailable',
                        'RPD quota setting is missing',
                        '503 RequestsPerDay diagnostic unavailable'):
            with self.subTest(message=message):
                info = coordinator.register_api_error(GEN, RuntimeError(message))
                self.assertFalse(info['daily_exhausted'])
                self.assertEqual(coordinator._DAILY_EXHAUSTED, {})
        error = RuntimeError('EmbedRequestsPerDayPerProjectPerModel')
        error.code = 429
        self.assertTrue(coordinator.analyze_api_error(error)['daily_exhausted'])

    def test_token_counts_and_request_ids_are_not_http_status_codes(self):
        for message in ('token estimate 1403 exceeds limit 250300',
                        'request_id abc429def: RPD quota lookup failed',
                        'request_id 401abc unavailable_metadata',
                        'input length 14290; expected 15030'):
            with self.subTest(message=message):
                info = coordinator.analyze_api_error(RuntimeError(message))
                self.assertFalse(info['auth_error'])
                self.assertFalse(info['rate_limited'])
                self.assertFalse(info['daily_exhausted'])
        error = RuntimeError('Request IDs: 401 403; input count 1503; requests per minute')
        error.code = 429
        info = coordinator.analyze_api_error(error)
        self.assertTrue(info['rate_limited'])
        self.assertFalse(info['auth_error'])
        self.assertFalse(info['daily_exhausted'])
        for status in (401, '403'):
            error = RuntimeError('Provider rejected the credential')
            error.status_code = status
            self.assertTrue(coordinator.analyze_api_error(error)['auth_error'])

    def test_read_only_input_planning_does_not_create_usage_or_reserve_capacity(self):
        self.assertEqual(coordinator.input_token_budget(GEN), 600)
        self.assertEqual(coordinator.input_token_budget(EMBED), 1200)
        self.assertEqual(coordinator.input_token_budget('unknown-model'), 0)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.window.exists())
        coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        daily_before, minute_before = self.state.read_bytes(), self.window.read_bytes()
        self.assertEqual(coordinator.input_token_budget(GEN), 600)
        self.assertEqual(self.state.read_bytes(), daily_before)
        self.assertEqual(self.window.read_bytes(), minute_before)

    def test_read_only_input_planning_observes_penalty_without_consuming_request(self):
        coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        before = self.window.read_bytes()
        self.assertEqual(coordinator.input_token_budget(GEN), 300)
        self.assertEqual(self.window.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_request_rate_only_keeps_document_capacity_and_durable_cooldown(self):
        for _ in range(2):
            coordinator.register_api_error(GEN, RuntimeError('429 RequestsPerMinute retryDelay: 20s'))
        self.assertEqual(coordinator.input_token_budget(GEN), 600)
        self.assertEqual(coordinator.input_token_capacity(GEN), 600)
        self.assertEqual(self.minute()['penalties'][GEN]['factor'], .25)
        self.assertEqual(self.minute()['penalties'][GEN]['token_factor'], 1)
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
        self.assertEqual(self.daily()['calls'], {})

    def test_temporary_token_capacity_waits_then_reserves_without_ledger_reset(self):
        coordinator.register_api_error(GEN, RuntimeError('429 TokensPerMinute retryDelay: 20s'))
        self.assertEqual(coordinator.input_token_budget(GEN), 300)
        self.assertEqual(coordinator.input_token_capacity(GEN), 600)
        with self.assertRaises(coordinator.AiTemporaryRateLimit) as raised:
            coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
        self.assertGreaterEqual(raised.exception.seconds, 3600)
        self.assertIsNone(coordinator._STATE_WARNING)
        self.assertEqual(self.daily()['calls'], {})
        with patch.object(time, 'time', return_value=NOW + 3601):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
            self.assertEqual(coordinator.input_token_budget(GEN), 600)
        self.assertEqual(self.daily()['calls'], {GEN: 1})

    def test_legacy_penalty_not_loosened_by_request_only_error(self):
        self.window.write_text(json.dumps({'version': 1, 'entries': [], 'penalties': {
            GEN: dict(until=NOW+3600, cooldown_until=NOW+20, factor=.25)}}), encoding='utf-8')
        coordinator.register_api_error(GEN, RuntimeError('429 RequestsPerMinute retryDelay: 20s'))
        self.assertEqual(coordinator.input_token_budget(GEN), 150)
        self.assertEqual(self.minute()['penalties'][GEN]['token_factor'], .25)
        self.assertEqual(self.minute()['penalties'][GEN]['factor'], .125)

    def test_capacity_fails_closed_on_damaged_ledger(self):
        self.window.write_text('{bad-minute', encoding='utf-8')
        before = self.window.read_bytes()
        self.assertEqual(coordinator.input_token_capacity(GEN), 0)
        self.assertEqual(self.window.read_bytes(), before)

    def test_baseline_oversize_is_still_terminal_under_temporary_penalty(self):
        coordinator.register_api_error(GEN, RuntimeError('429 TokensPerMinute'))
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=601)
        self.assertEqual(self.daily()['calls'], {})

    def test_read_only_input_planning_fails_closed_on_damage_or_lock_contention(self):
        self.window.write_text('{bad-minute', encoding='utf-8')
        before = self.window.read_bytes()
        self.assertEqual(coordinator.input_token_budget(GEN), 0)
        self.assertEqual(self.window.read_bytes(), before)
        owner = peer_for(self.state, 'dated_planning_lock_peer')
        with owner._quota_locked():
            start = time.monotonic()
            self.assertEqual(coordinator.input_token_budget(GEN), 0)
            self.assertLess(time.monotonic()-start, .5)
        self.assertFalse(self.state.exists())

    def test_read_only_input_planning_uses_unconfirmed_provisional_limits(self):
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        self.assertEqual(coordinator.input_token_budget(GEN), 6000)
        self.assertEqual(coordinator.input_token_budget(EMBED), 10000)
        self.assertEqual(coordinator.input_token_budget('unknown-model'), 0)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.window.exists())

    def test_simultaneous_independent_owners_cannot_overreserve_minute_budget(self):
        barrier = threading.Barrier(6)
        peers = [peer_for(self.state, f'dated_thread_peer_{number}') for number in range(6)]
        def reserve(owner):
            barrier.wait(timeout=5)
            until = time.monotonic() + 5
            while True:
                try:
                    owner.reserve_sync_model_slot(GEN, input_tokens=100)
                    return 'reserved'
                except owner._QuotaStateBusy:
                    if time.monotonic() > until:
                        raise
                    time.sleep(.005)
                except owner._RateWindowWait:
                    return 'wait'
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(reserve, peers))
        self.assertEqual(outcomes.count('reserved'), 2)
        self.assertEqual(outcomes.count('wait'), 4)
        self.assertEqual(self.daily()['calls'], {GEN: 2})
        self.assertEqual(len(self.minute()['entries']), 2)

    async def test_cancellation_while_shared_rate_waits_does_not_reserve_or_leak(self):
        coordinator.reserve_sync_model_slot(GEN, input_tokens=500)
        task = asyncio.create_task(coordinator.acquire_model_slot(GEN, input_tokens=101))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(coordinator._MODEL_WAITERS[GEN], 0)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        self.assertEqual(len(self.minute()['entries']), 1)

    async def test_waiting_request_observes_peer_provider_hold_before_dispatch(self):
        # Fill only the short process-local queue; shared-minute capacity is
        # still available. A later provider hold must win when the waiter wakes.
        with patch.dict('os.environ', {'AI_GLOBAL_GENERATE_RPM': '1',
                                      'AI_GLOBAL_RATE_PERIOD_SEC': '0.2'}):
            await coordinator.acquire_model_slot(GEN, input_tokens=100)
            waiting = asyncio.create_task(coordinator.acquire_model_slot(GEN, input_tokens=100))
            await asyncio.sleep(.01)
            self.assertFalse(waiting.done())
            peer = peer_for(self.state, 'provider_hold_while_waiting_peer')
            peer.register_api_error(GEN, RuntimeError('429 GenerateRequestsPerDayPerProjectPerModel'))
            with self.assertRaises(coordinator.AiDailyQuotaExhausted):
                await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(self.daily()['calls'], {GEN: 1})
        self.assertEqual(len(self.minute()['entries']), 1)
        self.assertEqual(coordinator._MODEL_WAITERS[GEN], 0)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=100)
        self.assertEqual(self.daily()['calls'], {GEN: 1, EMBED: 1})


if __name__ == '__main__':
    unittest.main()

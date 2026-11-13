"""App-conservative batch units, not measured provider batch quota; API zero."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
KEY = 'synthetic-weighted-quota-key-not-a-credential'


class TestWeightedQuota(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'ai_quota_state.json'
        self.window = Path(str(self.state) + '.minute.json')
        self.config = self.root / 'config.json'
        self.data = {'GOOGLE_API_KEY': KEY, 'AI_DAILY_REQUEST_LIMIT': 200,
            rate.POLICY_KEY: rate.observe({
                GEN: {'rpm': 15, 'tpm': 10000, 'rpd': 100},
                EMBED: {'rpm': 100, 'tpm': 30000, 'rpd': 100}},
                key=KEY, current_date=DAY)}
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

    def seed(self, calls, exhausted=None):
        self.state.write_text(json.dumps({'date_pacific': DAY.isoformat(),
            'calls': calls, 'exhausted': exhausted or {}}), encoding='utf-8')

    def counts(self):
        return json.loads(self.state.read_text(encoding='utf-8'))['calls']

    def peer(self, name):
        spec = importlib.util.spec_from_file_location(name, coordinator.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._state_path = lambda: str(self.state)
        module._pacific_today = lambda: DAY.isoformat()
        return module

    async def test_request_units_require_strict_bounded_integers_before_any_write(self):
        for units in (0, -1, True, False, 1.0, '2', None, 1001):
            with self.subTest(units=repr(units)):
                with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
                    coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=units)
                with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
                    await coordinator.acquire_model_slot(EMBED, input_tokens=10, request_units=units)
                with self.assertRaises(ValueError):
                    rate.window_admission({'entries': [], 'penalties': {}}, EMBED, 10,
                        {'rpm': 60, 'tpm': 1000}, request_units=units)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.window.exists())
        self.assertEqual(rate.validate_request_units(1000), 1000)

    def test_sync_batch_splits_minute_units_and_preserves_exact_total_tokens(self):
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.counts(), {EMBED: 3})
        rows = json.loads(self.window.read_text(encoding='utf-8'))['entries']
        self.assertEqual(rows, [[EMBED, NOW, 4], [EMBED, NOW, 3], [EMBED, NOW, 3]])
        self.assertEqual(sum(row[2] for row in rows), 10)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=1, request_units=3)
        rows = json.loads(self.window.read_text(encoding='utf-8'))['entries']
        self.assertEqual([row[2] for row in rows[-3:]], [1, 0, 0])
        self.assertEqual(self.counts()[EMBED], 6)

    async def test_async_batch_uses_weighted_local_and_shared_counts(self):
        self.assertFalse(await coordinator.acquire_model_slot(EMBED, input_tokens=33, request_units=3))
        self.assertEqual(self.counts()[EMBED], 3)
        self.assertEqual(len(coordinator._MODEL_CALLS[EMBED]), 3)
        self.assertEqual(len(json.loads(self.window.read_text())['entries']), 3)

    def test_legacy_global_cap_does_not_reject_or_partially_reserve_batch(self):
        self.data['AI_DAILY_REQUEST_LIMIT'] = 5
        self.save_config()
        self.seed({GEN: 2, EMBED: 2})
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=2)
        self.assertEqual(self.counts(), {GEN: 2, EMBED: 4})
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=1)
        self.assertEqual(self.counts(), {GEN: 2, EMBED: 5})
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)

    def test_observed_model_rpd_does_not_block_batch_or_other_model(self):
        self.data[rate.POLICY_KEY]['models'][EMBED]['rpd'] = 5
        self.save_config()
        self.seed({EMBED: 3})
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=2)
        coordinator.reserve_sync_model_slot(GEN, input_tokens=10, request_units=2)
        self.assertEqual(self.counts(), {EMBED: 5, GEN: 2})

    def test_previous_single_count_ledger_is_preserved_not_migrated_to_zero(self):
        self.seed({GEN: 7, EMBED: 5})
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.counts(), {GEN: 7, EMBED: 8})
        coordinator._reset_for_tests()
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10)
        self.assertEqual(self.counts(), {GEN: 7, EMBED: 9})

    async def test_batch_larger_than_rpm_is_immediate_hold_not_endless_wait(self):
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            await asyncio.wait_for(coordinator.acquire_model_slot(EMBED,
                input_tokens=10, request_units=61), timeout=.5)
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=61)
        self.assertFalse(self.window.exists())
        self.assertEqual(sum(coordinator._DAILY_CALLS.values()), 0)

    def test_minute_capacity_waits_for_enough_units_not_one_slot(self):
        window = {'entries': [[EMBED, NOW - 40, 1], [EMBED, NOW - 20, 1],
                              [EMBED, NOW - 10, 1]], 'penalties': {}}
        decision = rate.window_admission(window, EMBED, 2,
            {'rpm': 4, 'tpm': 100}, now=NOW, request_units=3)
        self.assertAlmostEqual(decision['wait'], 40.05)
        self.assertFalse(decision['too_large'])

    def test_model_minute_admission_counts_every_batch_unit(self):
        self.data[rate.POLICY_KEY]['models'][EMBED]['rpm'] = 6  # effective 4
        self.save_config()
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        before = self.state.read_bytes(), self.window.read_bytes()
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=2)
        self.assertEqual((self.state.read_bytes(), self.window.read_bytes()), before)
        self.assertIsNone(coordinator._STATE_WARNING)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10)
        self.assertEqual(self.counts()[EMBED], 4)

    def test_planning_uses_minute_rpm_and_penalty_without_daily_cap_or_reservation(self):
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)
        self.assertEqual(coordinator.input_request_budget(GEN), 10)
        self.assertEqual(coordinator.input_request_budget('unobserved-model'), 0)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.window.exists())
        self.seed({EMBED: 77, GEN: 1})
        before = self.state.read_bytes()
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)
        self.assertEqual(self.state.read_bytes(), before)
        coordinator.register_api_error(GEN, RuntimeError('429 RESOURCE_EXHAUSTED'))
        minute = self.window.read_bytes()
        self.assertEqual(coordinator.input_request_budget(GEN), 5)
        self.assertEqual(self.window.read_bytes(), minute)
        self.assertEqual(self.state.read_bytes(), before)

    def test_unconfirmed_planning_and_batch_use_same_provisional_protection(self):
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        self.assertIsNone(coordinator.daily_request_limit())
        self.assertEqual(coordinator.input_request_budget(EMBED), 10)
        self.assertEqual(coordinator.input_request_budget(GEN), 2)
        self.assertEqual(coordinator.input_token_budget(EMBED), 10000)
        self.assertEqual(coordinator.input_token_budget(GEN), 6000)
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=10)
        self.assertEqual(self.counts()[EMBED], 10)
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=2)
        with patch.object(time, 'time', return_value=NOW + 61):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=10)
        self.assertEqual(self.counts()[EMBED], 20)
        with patch.object(time, 'time', return_value=NOW + 122):
            self.assertEqual(coordinator.input_request_budget(EMBED), 10)
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=10)
            coordinator.reserve_sync_model_slot(GEN, input_tokens=10)
        self.assertEqual(self.counts(), {EMBED: 30, GEN: 1})

    def test_first_unconfirmed_single_call_requires_estimate_and_known_model(self):
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        self.assertFalse(self.window.exists())
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot(GEN)
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=6001)
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            coordinator.reserve_sync_model_slot('unobserved-model', input_tokens=10)
        self.assertFalse(self.window.exists())
        self.assertEqual(sum(coordinator._DAILY_CALLS.values()), 0)
        coordinator.reserve_sync_model_slot(GEN, input_tokens=6000)
        self.assertEqual(self.counts(), {GEN: 1})
        self.assertEqual(json.loads(self.window.read_text())['entries'], [[GEN, NOW, 6000]])
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=1)

    async def test_unconfirmed_single_calls_share_minute_rpm_from_first_request(self):
        self.data.pop(rate.POLICY_KEY)
        self.save_config()
        await coordinator.acquire_model_slot(GEN, input_tokens=100)
        peer = self.peer('unconfirmed_single_peer')
        peer.reserve_sync_model_slot(GEN, input_tokens=100)
        before = self.state.read_bytes(), self.window.read_bytes()
        with self.assertRaises(coordinator._RateWindowWait):
            coordinator.reserve_sync_model_slot(GEN, input_tokens=100)
        self.assertEqual((self.state.read_bytes(), self.window.read_bytes()), before)
        self.assertEqual(self.counts()[GEN], 2)

    def test_planning_fails_closed_for_damage_holds_and_lock_contention(self):
        self.state.write_text('{damaged', encoding='utf-8')
        self.assertEqual(coordinator.input_request_budget(EMBED), 0)
        self.assertEqual(self.state.read_text(), '{damaged')
        self.assertFalse(list(self.root.glob('*.corrupt.*')))
        coordinator._reset_for_tests()
        self.seed({}, {EMBED: DAY.isoformat()})
        self.assertEqual(coordinator.input_request_budget(EMBED), 0)
        self.assertEqual(coordinator.input_request_budget(GEN), 10)
        owner = self.peer('weighted_planning_lock_peer')
        with owner._quota_locked():
            self.assertEqual(coordinator.input_request_budget(GEN), 0)

    def test_damaged_minute_planning_or_reservation_does_not_dispatch(self):
        self.window.write_text('{damaged', encoding='utf-8')
        self.assertEqual(coordinator.input_request_budget(EMBED), 0)
        with self.assertRaises(coordinator.AiQuotaStateUnavailable):
            coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=2)
        self.assertEqual(sum(coordinator._DAILY_CALLS.values()), 0)
        self.assertEqual(self.window.read_text(), '{damaged')

    def test_daily_write_failure_rolls_back_all_units_and_keeps_safe_minute_overcount(self):
        self.seed({EMBED: 2})
        coordinator._load_state()
        before = self.state.read_bytes()
        with patch.object(coordinator, '_save_state', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(coordinator._DAILY_CALLS[EMBED], 2)
        self.assertEqual(len(json.loads(self.window.read_text())['entries']), 3)

    def test_minute_write_failure_reserves_zero_daily_units(self):
        self.seed({EMBED: 2})
        coordinator._load_state()
        original = coordinator._atomic_json
        def fail_minute(path, payload):
            if str(path).endswith('.minute.json'):
                raise OSError('synthetic minute failure')
            return original(path, payload)
        with patch.object(coordinator, '_atomic_json', side_effect=fail_minute):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.counts()[EMBED], 2)
        self.assertFalse(self.window.exists())

    def test_independent_owners_preserve_all_batches_despite_legacy_daily_cap(self):
        self.data['AI_DAILY_REQUEST_LIMIT'] = 5
        self.save_config()
        peers = [self.peer(f'weighted_owner_{number}') for number in range(4)]
        barrier = threading.Barrier(len(peers))
        def reserve(peer):
            barrier.wait()
            deadline = time.monotonic() + 2
            while True:
                try:
                    peer.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
                    return 'reserved'
                except peer._QuotaStateBusy:
                    if time.monotonic() >= deadline:
                        return 'busy'
                    time.sleep(.005)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(reserve, peers))
        self.assertEqual(results.count('reserved'), 4)
        self.assertEqual(self.counts()[EMBED], 12)
        self.assertEqual(len(json.loads(self.window.read_text())['entries']), 12)

    def test_rate_penalty_reduces_batch_capacity_and_persists_across_restart(self):
        coordinator.register_api_error(EMBED, RuntimeError('429 RESOURCE_EXHAUSTED'))
        self.assertEqual(coordinator.input_request_budget(EMBED), 30)
        peer = self.peer('weighted_penalty_peer')
        self.assertEqual(peer.input_request_budget(EMBED), 30)
        with self.assertRaises(peer.AiTemporaryRateLimit):
            peer.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=31)
        self.assertEqual(sum(peer._DAILY_CALLS.values()), 0)

    def test_reserve_false_rechecks_all_batch_units_without_consuming(self):
        self.seed({EMBED: 3})
        before = self.state.read_bytes()
        coordinator._reserve_daily_slot(EMBED, reserve=False, input_tokens=10, request_units=4)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.window.exists())
        self.data['AI_DAILY_REQUEST_LIMIT'] = 5
        self.save_config()
        coordinator._reserve_daily_slot(EMBED, reserve=False, input_tokens=10, request_units=4)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.window.exists())

    def test_large_existing_daily_count_is_retained_without_20_or_1000_cap(self):
        self.data['AI_DAILY_REQUEST_LIMIT'] = 20
        self.data[rate.POLICY_KEY]['models'][EMBED]['rpd'] = 0
        self.save_config()
        self.seed({GEN: 1001, EMBED: 2050})
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.counts(), {GEN: 1001, EMBED: 2053})
        self.assertEqual(coordinator.input_request_budget(EMBED), 60)
        status = coordinator.status()
        self.assertEqual(status['recorded_requests_today'], 3054)
        self.assertIsNone(status['daily_request_limit'])
        self.assertIsNone(status['local_budget_remaining'])
        self.assertFalse(status['local_budget_exhausted'])

    def test_expired_observation_does_not_reinstate_20_daily_cap(self):
        self.data['AI_DAILY_REQUEST_LIMIT'] = 0
        self.data[rate.POLICY_KEY]['observed_on'] = (DAY-datetime.timedelta(days=7)).isoformat()
        self.save_config()
        self.seed({GEN: 25, EMBED: 1001})
        coordinator.reserve_sync_model_slot(EMBED, input_tokens=10, request_units=3)
        self.assertEqual(self.counts(), {GEN: 25, EMBED: 1004})
        self.assertEqual(coordinator.input_request_budget(EMBED), 10)
        self.assertIsNone(coordinator.daily_request_limit())

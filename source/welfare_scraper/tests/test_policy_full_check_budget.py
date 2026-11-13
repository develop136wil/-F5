"""Synthetic snapshots only: no config, operational DB, SDK or API I/O."""
import copy
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import policy_full_check_budget as budget

MODEL = 'gemini-3.5-flash-lite'


class TestPolicyFullCheckBudget(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            'local_budget_remaining': 15, 'local_budget_exhausted': False,
            'models': {MODEL: {'session_calls': 5, 'daily_exhausted': False}},
            'state_warning': None,
            'quota_policy': {'state': 'current', 'effective': {
                MODEL: {'rpm': 10, 'tpm': 60000, 'rpd': 80}}},
        }
        self.client = Mock(return_value=object())
        self.status = Mock(side_effect=lambda: copy.deepcopy(self.snapshot))
        self.allowance = Mock(return_value=10)
        for patcher in (
            patch.dict(sys.modules, {'ai_classifier': SimpleNamespace(get_client=self.client)}),
            patch.dict(os.environ, {'CLASSIFY_MODEL': MODEL}),
            patch.object(budget.ai_coordinator, 'status', self.status),
            patch.object(budget.ai_coordinator, 'input_request_budget', self.allowance),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reports_unknown_provider_remaining_and_does_not_mutate_snapshot(self):
        before = copy.deepcopy(self.snapshot)
        self.assertEqual(budget.public_ai_budget(), {'remaining': None, 'reason': ''})
        self.assertEqual(self.snapshot, before)
        self.allowance.assert_called_once_with(MODEL)

    def test_no_client_requires_connection_without_reading_quota_state(self):
        self.client.return_value = None
        self.assertEqual(budget.public_ai_budget(), {
            'remaining': None, 'reason': 'ai_connection_required'})
        self.status.assert_not_called()
        self.allowance.assert_not_called()

    def test_existing_client_hint_does_not_construct_another_sdk_client(self):
        self.assertEqual(budget.ai_pause_reason(client_available=True), '')
        self.client.assert_not_called()

    def test_missing_client_hint_does_not_construct_another_sdk_client(self):
        self.assertEqual(budget.ai_pause_reason(client_available=False), 'ai_connection_required')
        self.client.assert_not_called()
        self.status.assert_not_called()

    def test_obsolete_local_daily_exhaustion_does_not_block_request_planning(self):
        self.snapshot['local_budget_remaining'] = 0
        self.snapshot['local_budget_exhausted'] = True
        self.assertEqual(budget.ai_pause_reason(), '')
        self.allowance.assert_called_once_with(MODEL)

    def test_provider_hold_stops_for_selected_model_and_wildcard_only(self):
        for held_model in (MODEL, '*'):
            with self.subTest(model=held_model):
                self.snapshot['models'] = {held_model: {'daily_exhausted': True}}
                self.assertEqual(budget.ai_pause_reason(), 'daily_limit')
        self.snapshot['models'] = {'other-model': {'daily_exhausted': True}}
        self.assertEqual(budget.ai_pause_reason(), '')

    def test_observed_model_daily_count_is_not_provider_exhaustion(self):
        self.snapshot['models'][MODEL]['session_calls'] = 1001
        self.assertEqual(budget.public_ai_budget(), {'remaining': None, 'reason': ''})
        self.allowance.assert_called_once_with(MODEL)

    def test_expired_observation_does_not_reintroduce_twenty_per_day_limit(self):
        self.snapshot['quota_policy'] = {'state': 'reconfirmation_required', 'effective': {}}
        self.snapshot['models'][MODEL]['session_calls'] = 20
        self.assertEqual(budget.ai_pause_reason(), '')

    def test_occupied_minute_and_cooldown_keep_work_running(self):
        self.snapshot['models'][MODEL].update(recent_calls=10, waiting=2, cooldown_seconds=45)
        self.assertEqual(budget.ai_pause_reason(), '')

    def test_ledger_warning_takes_precedence_over_reported_exhaustion(self):
        self.snapshot.update(state_warning='synthetic unreadable ledger', local_budget_exhausted=True)
        self.assertEqual(budget.ai_pause_reason(), 'quota_state_unavailable')

    def test_unknown_planning_state_stops_without_guessing_daily_quota(self):
        for value in (0, None, False, '1'):
            with self.subTest(allowance=value):
                self.allowance.return_value = value
                self.assertEqual(budget.ai_pause_reason(), 'quota_state_unavailable')
        self.allowance.side_effect = OSError('synthetic ledger failure')
        self.assertEqual(budget.ai_pause_reason(), 'quota_state_unavailable')

    def test_missing_or_malformed_status_is_not_admission(self):
        self.status.return_value = None
        self.status.side_effect = None
        self.assertEqual(budget.ai_pause_reason(), 'quota_state_unavailable')
        for models in (None, 'unavailable', {MODEL: 'unavailable'}, {'*': 'unavailable'}):
            with self.subTest(models=models):
                self.status.return_value = {'models': models}
                self.assertEqual(budget.ai_pause_reason(), 'quota_state_unavailable')
        self.status.return_value = {'models': {}, 'local_budget_remaining': '15'}
        self.assertEqual(budget.ai_pause_reason(), '')

    def test_explicit_model_matches_callers_model_override(self):
        self.snapshot['models']['override-model'] = {'daily_exhausted': True}
        self.assertEqual(budget.ai_pause_reason('override-model'), 'daily_limit')
        self.assertEqual(budget.ai_pause_reason(), '')


if __name__ == '__main__':
    unittest.main()

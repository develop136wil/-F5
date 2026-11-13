import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import policy_sync as ps


class AcceptanceReceiptTests(unittest.TestCase):
    def setUp(self):
        self.accepted = {'field': '지원 내용', 'applied': True, 'acceptance': {
            'key': 'support_detail', 'written': '주차요금 50% 감면', 'extracted': '주차 요금 50% 할인'}}
        self.pending = {'fields': [self.accepted]}
        self.diffs = [{'field': '지원 내용'}, {'field': '신청 조건'}]
        self.current = {'support_detail': '주차요금 50% 감면'}
        self.extracted = {'support_detail': '주차 요금 50% 할인'}

    def test_same_accepted_pair_not_proposed_again(self):
        diffs, retained = ps._preserve_accepted_fields(self.diffs, self.pending, self.current, self.extracted)
        self.assertEqual(diffs, [{'field': '신청 조건'}])
        self.assertEqual(retained, [self.accepted])

    def test_real_source_change_remains_visible(self):
        for value in ['주차 요금 30% 할인', '', None]:
            diffs, _ = ps._preserve_accepted_fields(self.diffs, self.pending, self.current, {'support_detail': value})
            self.assertEqual(diffs, self.diffs)

    def test_manual_notion_edit_remains_visible(self):
        diffs, _ = ps._preserve_accepted_fields(self.diffs, self.pending, {'support_detail': '별도 수정'}, self.extracted)
        self.assertEqual(diffs, self.diffs)

    def test_missing_receipt_or_baseline_is_not_guessed(self):
        for pending, extracted in [({'fields': [{'field': '지원 내용', 'applied': True}]}, self.extracted), (self.pending, {})]:
            diffs, _ = ps._preserve_accepted_fields(self.diffs, pending, self.current, extracted)
            self.assertEqual(diffs, self.diffs)

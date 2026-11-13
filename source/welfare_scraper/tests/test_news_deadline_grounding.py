"""Offline deadline regressions: explicit clocks, event metadata and invalid dates.

Snapshot checks read existing public evaluation evidence without modifying it.
Synthetic cases test guards; neither is an additional model accuracy trial.
"""
import copy
from pathlib import Path
import unittest

from services.news_analysis_safety import _date_supported, assess
from services.news_deadline_grounding import deadline_calendar_day
from services.news_profile import NewsProfile


class TestNewsDeadlineGrounding(unittest.TestCase):
    def test_complete_application_endpoints(self):
        cases = [
            ('신청 마감 2026-10-31.', '2026-10-31'),
            ('접수 마감일시: 2026-10-31 18:00', '2026-10-31 18:00'),
            ('접수 마감일시: 2026-10-31 18:00', '2026-10-31T18:00'),
            ('접수 마감일시: 2026-10-31T18:00', '2026-10-31 18:00'),
            ('모집기간: 2026-10-01 ~ 2026-10-31', '2026-10-31'),
            ('신청기간: 2026-10-01 ~ 10-31 18:00', '2026-10-31 18:00'),
            ('신청기간: 2026.10.1.(목) ~ 2026년 10월 31일(토) 18시 30분까지', '2026-10-31 18:30'),
            ('접수는 2026년 10월 31일 18시까지', '2026-10-31 18:00'),
            ('신청 2026년 10월 31일까지', '2026-10-31'),
            ('접수기간\n2026-10-01 ~ 2026-10-31 18:00', '2026-10-31 18:00'),
            ('신청 마감: 2026-10-31 00:00', '2026-10-31 00:00'),
        ]
        for content, value in cases:
            with self.subTest(content=content, value=value):
                self.assertTrue(_date_supported(value, content))

    def test_no_inferred_year_time_or_timezone(self):
        cases = [
            ('신청 마감: 2026-10-31', '2026-10-31 18:00'),
            ('신청 마감: 2026-10-31 18:00', '2026-10-31 00:00'),
            ('신청 마감: 2026-10-31 18:00', '2026-10-31T09:00Z'),
            ('신청 마감: 2026-10-31 18:00', '2026-10-31T18:00+09:00'),
            ('신청 마감: 10월 31일 18:00', '2026-10-31 18:00'),
            ('신청 마감: 2026-10-31. 문의시간 18:00', '2026-10-31 18:00'),
            ('신청 마감: 2026-10-31. 교육일 2026-10-31 18:00', '2026-10-31 18:00'),
            ('신청기간: 2026-10-01 10:00 ~ 2026-10-31', '2026-10-31 10:00'),
            ('신청 마감: 2026-10-31. 18:00에 행사 시작', '2026-10-31 18:00'),
            ('신청 마감: 2026-10-31. 18:00 문의 가능', '2026-10-31 18:00'),
            ('신청 마감: 2026-11-01 00:00', '2026-10-31 24:00'),
        ]
        for content, value in cases:
            with self.subTest(content=content, value=value):
                self.assertFalse(_date_supported(value, content))

    def test_event_metadata_and_unrelated_dates_cannot_establish_deadline(self):
        cases = [
            '행사기간: 2026-10-01 ~ 2026-10-31 18:00',
            '게시기간: 2026-10-01 ~ 2026-10-31 18:00',
            '개인이 신청합니다. 행사는 2026-10-31 18:00까지',
            '신청은 2026-10-01부터이며 행사일은 2026-10-31 18:00입니다.',
            '신청일시\n2026-10-01 00:00 ~ 2026-10-31 18:00',
            '신청 방법: 방문. 행사 종료: 2026-10-31 18:00',
            '신청기간: 교육일 2026-10-31 18:00',
            '접수 마감: 2026-10-31 18:00 아님',
            '접수 마감: 2026-10-31 18:00\n아님',
            '접수 마감:\n2026-10-31 18:00\n아님',
            '변경 전 접수 마감: 2026-10-31 18:00',
        ]
        for content in cases:
            with self.subTest(content=content):
                self.assertFalse(_date_supported('2026-10-31 18:00', content))
                self.assertFalse(_date_supported('2026-10-31', content))

    def test_invalid_source_and_proposed_values(self):
        for value in (None, True, 20261031, '', '2026-02-30', '2026-13-01',
                      '2026-10-31 24:00', '2026-10-31 18:60',
                      '2026-10-31 18:00:99', '2026-1-1'):
            with self.subTest(value=value):
                self.assertFalse(_date_supported(value, f'신청 마감: {value}'))
        for content in ('접수 마감: 2026-02-30 18:00',
                        '접수 마감: 2026-10-31 25:00',
                        '접수 마감: 2026-10-31 18:60',
                        '접수 마감: 2026-10-31 18:00:99',
                        '접수기간: 2026-11-01 ~ 2026-10-31 18:00',
                        '접수기간: 2026-10-31 19:00 ~ 2026-10-31 18:00'):
            with self.subTest(content=content):
                self.assertFalse(_date_supported('2026-10-31 18:00', content))
                self.assertFalse(_date_supported('2026-10-31', content))
        self.assertFalse(_date_supported('2026-10-31', None))

    def test_calendar_consumer_uses_same_strict_date_contract(self):
        for value in ('2026-10-31', '2026-10-31 18:00', '2026-10-31T18:00'):
            with self.subTest(value=value):
                self.assertEqual(deadline_calendar_day(value).isoformat(), '2026-10-31')
        for value in (None, True, '2026-10-31 24:00', '2026-02-30',
                      '2026-10-31 18:00:00', '2026-10-31T18:00+09:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deadline_calendar_day(value)

    def test_provider_snapshot_preserves_correct_date_and_time(self):
        source = self._snapshot('news-provider.txt')
        self.assertTrue(_date_supported('2025-12-24 18:00', source))
        self.assertTrue(_date_supported('2025-12-24', source))
        for wrong in ('2026-12-24 18:00', '2025-12-16 18:00', '2025-12-24 12:00'):
            with self.subTest(wrong=wrong):
                self.assertFalse(_date_supported(wrong, source))

    def test_market_snapshot_metadata_stays_unverified(self):
        source = self._snapshot('news-market.txt')
        for wrong in ('2026-09-15 00:00', '2026-09-15 17:00', '2026-09-15'):
            with self.subTest(wrong=wrong):
                self.assertFalse(_date_supported(wrong, source))

    def test_full_safety_preserves_clock_without_mutation(self):
        facts = dict(is_relevant=True, category='복지', target='개인', residence=[],
                     life_cycle=[], target_characteristics=[], interest_topics=[],
                     start_age_months=None, end_age_months=None, evidence_snippet='',
                     residence_evidence=[], life_cycle_evidence=[],
                     applicant_type='individual', applicant_evidence='개인이 신청합니다.',
                     support_detail='', eligibility_detail='', contact='',
                     deadline='2026-10-31 18:00')
        before = copy.deepcopy(facts)
        source = '개인이 신청합니다.\n접수 마감: 2026-10-31 18:00'
        result = assess(NewsProfile('p', 1, '시험', '전국'), facts, source)
        self.assertEqual(result['analysis']['deadline'], '2026-10-31 18:00')
        self.assertNotIn('deadline', result['analysis']['unverified_facts'])
        self.assertEqual(facts, before)
        wrong = assess(NewsProfile('p', 1, '시험', '전국'), facts,
                       '개인이 신청합니다.\n행사 종료: 2026-10-31 18:00')
        self.assertEqual(wrong['analysis']['deadline'], '')
        self.assertEqual(wrong['analysis']['unverified_facts']['deadline'], before['deadline'])

    @staticmethod
    def _snapshot(name):
        path = (Path(__file__).resolve().parents[2] / 'artifacts' /
                'public-document-eval-20260909' / 'sources' / name)
        return path.read_text(encoding='utf-8')

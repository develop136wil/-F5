"""Offline multi-session deadline guards; no model or operating-data access."""
import copy
import hashlib
from pathlib import Path
import unittest

from services.news_analysis_safety import assess
from services.news_deadline_grounding import deadline_supported
from services.news_profile import NewsProfile


PERIOD = '신청기간: 2026-10-01 10:00 ~ 2026-10-31 18:00'
DEADLINE = '2026-10-31 18:00'


class MultiSessionDeadline(unittest.TestCase):
    def test_full_year_periods_cannot_choose_first_or_last_session(self):
        source = (PERIOD + '\n2회차 신청기간: 2026-11-01 10:00 ~ 2026-11-30 18:00')
        for value in (DEADLINE, '2026-10-31', '2026-11-30 18:00', '2026-11-30'):
            with self.subTest(value=value):
                self.assertFalse(deadline_supported(value, source))
                self.assertFalse(deadline_supported(value, '\n'.join(reversed(source.splitlines()))))

    def test_separate_routes_and_same_day_different_clocks_remain_ambiguous(self):
        for other in ('방문 접수 마감: 2026-10-30 18:00',
                      '우편 접수 마감: 2026-10-31 17:00',
                      '온라인 신청 마감: 2026-10-31',
                      '추가 접수기간: 2026-11-01 ~ 11-02 18시',
                      '추가 접수기간: 11-01 ~ 11-02',
                      '추가 접수기간\n: 11-01 ~ 11-02',
                      '추가 접수기간: 2026-11-01 ~ 2026-11-02 25시'):
            with self.subTest(other=other):
                self.assertFalse(deadline_supported(DEADLINE, PERIOD + '\n' + other))
                self.assertFalse(deadline_supported('2026-10-31', other + '\n' + PERIOD))

    def test_identical_repeated_endpoint_does_not_create_another_deadline(self):
        for other in (PERIOD, '접수 마감: 2026년 10월 31일 18시',
                      '우편 접수 마감: 2026-10-31 18:00'):
            with self.subTest(other=other):
                self.assertTrue(deadline_supported(DEADLINE, PERIOD + '\n' + other))
                self.assertTrue(deadline_supported('2026-10-31', PERIOD + '\n' + other))
        self.assertTrue(deadline_supported('2027-01-03 16:00',
            '접수기간: 2026.12.20.~2027.1.3.16시'))

    def test_relative_per_session_and_undecided_extra_routes_are_not_ignored(self):
        for other in ('신청기간: 각 교육일 7일 전까지',
                      '신청기간\n매 교육일 7일 전까지',
                      '신청기간: 2026년 7월 6일 월요일 10시 ~ 매 교육일 7일 전까지',
                      '회차별 접수기간: 추후 공지',
                      '추가 접수는 추후 별도 안내',
                      '추가 모집기간: 미정',
                      '별도 접수기간: 상시'):
            with self.subTest(other=other):
                self.assertFalse(deadline_supported(DEADLINE, PERIOD + '\n' + other))
                self.assertFalse(deadline_supported(DEADLINE, other + '\n' + PERIOD))

    def test_cancelled_or_denied_relative_extra_route_does_not_hide_single_period(self):
        for other in ('추가 접수기간은 추후 공지가 아닙니다.',
                      '변경 전 추가 접수기간: 상시',
                      '변경 전\n추가 접수기간: 상시',
                      '추가 모집기간: 미정 아님',
                      '별도 상시 접수는 없습니다.'):
            with self.subTest(other=other):
                self.assertTrue(deadline_supported(DEADLINE, PERIOD + '\n' + other))

    def test_available_onsite_route_is_not_erased_by_a_full_year_period(self):
        for other in ('현장 접수 가능', '현장 접수 가능합니다.',
                      '행사 당일 현장 접수 가능', '현장에서 접수도 가능'):
            with self.subTest(other=other):
                self.assertFalse(deadline_supported(DEADLINE, PERIOD + '\n' + other))

    def test_denied_old_example_or_conditional_onsite_route_is_not_asserted(self):
        for other in ('현장 접수 가능하지 않습니다.', '현장 접수 불가',
                      '현장 접수 가능 아님', '현장 접수 가능\n아님',
                      '현장 접수 가능\n\n아님',
                      '변경 전 현장 접수 가능', '예시: 현장 접수 가능',
                      '현장 접수 가능 여부는 별도 문의해주세요.',
                      '현장 접수 가능한 경우에만 방문하세요.'):
            with self.subTest(other=other):
                self.assertTrue(deadline_supported(DEADLINE, PERIOD + '\n' + other))

    def test_explicit_closed_application_declaration_withholds_future_deadline(self):
        for other in ('접수가 마감되었습니다.', '신청은 종료됐습니다.',
                      '현재 모든 회차의 신청과 대기는 마감되었습니다.',
                      '현재 접수가 조기 마감되었습니다.',
                      '접수 상태: 마감', '신청현황\n종료', '모집 상태:\n마감'):
            with self.subTest(other=other):
                self.assertFalse(deadline_supported(DEADLINE, PERIOD + '\n' + other))

    def test_deadline_labels_and_possible_or_denied_closure_are_not_actual_closure(self):
        for other in ('접수 마감일: 2026-10-31 18:00',
                      '신청방법: 온라인 신청(선착순 마감)',
                      '모집은 선착순으로 조기 마감될 수 있습니다.',
                      '접수가 마감되지 않았습니다.', '접수 마감 아님',
                      '접수 상태: 마감 아님', '접수 상태\n마감\n아님',
                      '접수 상태\n\n마감\n\n아님',
                      '예시: 접수가 마감되었습니다.',
                      '변경 전 접수가 마감되었습니다.',
                      '행사가 종료되었습니다.', '댓글 상태: 마감',
                      '문의 방법\n마감', '마감 안내 문구를 확인하세요.'):
            with self.subTest(other=other):
                self.assertTrue(deadline_supported(DEADLINE, PERIOD + '\n' + other))

    def test_event_dates_and_page_metadata_do_not_compete_with_one_application_period(self):
        source = PERIOD + ('\n행사기간: 2026-11-01 ~ 2026-11-30'
                           '\n게시일: 2026-09-01\n수정일: 2026-09-02'
                           '\n교육은 회차별로 진행합니다.')
        self.assertTrue(deadline_supported(DEADLINE, source))

    def test_assess_keeps_original_and_full_source_but_withholds_only_unverified_date(self):
        source = ('개인이 신청합니다.\n' + PERIOD +
                  '\n2회차 접수 마감: 2026-11-30 18:00')
        facts = dict(is_relevant=True, category='복지', target='개인', residence=[],
                     life_cycle=[], target_characteristics=[], interest_topics=[],
                     start_age_months=None, end_age_months=None, evidence_snippet='',
                     residence_evidence=[], life_cycle_evidence=[], applicant_type='individual',
                     applicant_evidence='개인이 신청합니다.', support_detail=source,
                     eligibility_detail='', contact='', deadline=DEADLINE)
        before = copy.deepcopy(facts)
        result = assess(NewsProfile('p', 1, '시험', '전국'), facts, source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['deadline'], '')
        self.assertEqual(result['analysis']['unverified_facts']['deadline'], DEADLINE)
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(facts, before)

    def test_frozen_memorial_sessions_do_not_become_one_programme_deadline(self):
        path = (Path(__file__).resolve().parents[2] / 'artifacts' /
                'ai-holdout-prep-20260913' / 'sources' / 'news-memorial-school.txt')
        original = path.read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         'fabf744aa848e01ab473c4eb341c85367fb1dd89219f929bb4fd397d95e1506e')
        source = original.decode('utf-8')
        self.assertIn('매 교육일 7일 전까지', source)
        self.assertIn('마감', source)
        for value in ('2026-11-24 17:00', '2026-11-24', '2026-09-01 17:00'):
            with self.subTest(value=value):
                self.assertFalse(deadline_supported(value, source))
        self.assertEqual(path.read_bytes(), original)

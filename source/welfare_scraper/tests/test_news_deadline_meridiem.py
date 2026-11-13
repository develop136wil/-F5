"""Offline Korean AM/PM deadline checks; no model, network or operating DB.

The public source is read with a frozen hash, not modified or fetched. These
are parser/validation regressions, not a new measurement of model accuracy.
"""
import copy
import hashlib
from pathlib import Path
import unittest

from services.news_deadline_grounding import deadline_supported


PERIOD = '모집기간: 2026. 8. 4.(화) 오전 10시 ~ 8. 25.(화)'


class TestNewsDeadlineMeridiem(unittest.TestCase):
    def test_explicit_start_clock_does_not_invent_an_end_clock(self):
        for source in (PERIOD, PERIOD + ' [선착순]',
                       '접수기간\n2026.8.4.오전10시~8.25.',
                       '신청기간: 2026년 8월 4일 오전 10시부터 8월 25일까지',
                       '접수기간: 2026-08-04 오전 10:00 - 2026-08-25'):
            with self.subTest(source=source):
                self.assertTrue(deadline_supported('2026-08-25', source))
                for clock in ('00:00', '10:00', '12:00', '23:59'):
                    self.assertFalse(deadline_supported('2026-08-25 ' + clock, source))
                self.assertFalse(deadline_supported('2026-08-04 10:00', source))

    def test_complete_and_shared_year_end_clocks_are_equivalent(self):
        for endpoint in ('2026.8.25.(화) 오후 3시 30분',
                         '8.25.(화) 오후 3시 30분',
                         '2026년 8월 25일 오후3:30', '8월25일 오후3:30'):
            source = '신청기간: 2026.8.4. 오전10시 ~ ' + endpoint
            with self.subTest(endpoint=endpoint):
                self.assertTrue(deadline_supported('2026-08-25 15:30', source))
                self.assertTrue(deadline_supported('2026-08-25T15:30', source))
                self.assertTrue(deadline_supported('2026-08-25', source))
                self.assertFalse(deadline_supported('2026-08-25 03:30', source))
                self.assertFalse(deadline_supported('2026-08-25 10:00', source))

    def test_twelve_hour_boundaries_and_literal_minutes(self):
        for printed, expected in (
                ('오전 12시', '00:00'), ('오전12:01', '00:01'),
                ('오후 12시', '12:00'), ('오후12시 59분', '12:59'),
                ('오전 1시 2분', '01:02'), ('오후 1시2분', '13:02'),
                ('오전11시59분', '11:59'), ('오후11:59', '23:59')):
            with self.subTest(printed=printed):
                for label in ('접수 마감일시:', '신청기한:', '모집 종료일:'):
                    source = label + ' 2026-08-25 ' + printed
                    self.assertTrue(deadline_supported('2026-08-25 ' + expected, source))
                    self.assertTrue(deadline_supported('2026-08-25', source))

    def test_single_application_point_still_needs_until_without_deadline_label(self):
        self.assertTrue(deadline_supported('2026-08-25 15:00',
            '신청은 2026년 8월 25일 오후 3시까지'))
        for label in ('신청:', '접수:', '신청일시:', '신청일시\n'):
            with self.subTest(label=label):
                self.assertFalse(deadline_supported('2026-08-25 15:00',
                    label + ' 2026년 8월 25일 오후 3시'))

    def test_malformed_meridiem_never_falls_back_to_a_day(self):
        clocks = ('오전', '오후', '오전 0시', '오후 00:00', '오전 13시',
                  '오후 24시', '오후 25:00', '오전 9:60', '오후 3시 60분',
                  '오전 10', '오후 3:', '오전 10:1', '오후 3:300',
                  '오전 오전 10시', '오전 오후 10시', '오후 3시 오전',
                  '오후 3시 30', '오전 10:00:00', '오후 3시 30분 15초',
                  '오후 3시 반', '오후 3시30분 반', '오후 T3:00',
                  '오후 3:00Z', '오전10시+09:00', '오후3시-09:00',
                  '오후 3시 PM', '오후 3시 자정')
        for clock in clocks:
            for source in ('접수 마감: 2026.8.25. ' + clock,
                           '모집기간: 2026.8.4. 오전10시 ~ 8.25. ' + clock,
                           '모집기간: 2026.8.4. ' + clock + ' ~ 8.25.'):
                with self.subTest(source=source):
                    self.assertFalse(deadline_supported('2026-08-25', source))
                    self.assertFalse(deadline_supported('2026-08-25 15:00', source))

    def test_plain_twenty_four_hour_times_remain_supported(self):
        for printed, expected in (('00:00', '00:00'), ('12시', '12:00'),
                                  ('15시30분', '15:30'), ('23:59', '23:59')):
            source = '접수기간: 2026.8.4.10시~8.25. ' + printed
            with self.subTest(printed=printed):
                self.assertTrue(deadline_supported('2026-08-25 ' + expected, source))

    def test_meridiem_is_not_borrowed_by_an_unprefixed_end(self):
        source = '모집기간: 2026.8.4. 오후3시 ~ 8.25. 6시'
        self.assertTrue(deadline_supported('2026-08-25 06:00', source))
        self.assertFalse(deadline_supported('2026-08-25 18:00', source))

    def test_same_day_order_uses_converted_clocks_without_rollover(self):
        for start, end, expected in (
                ('오전12시', '오전1시', '01:00'),
                ('오전11시', '오후12시', '12:00'),
                ('오후12시', '오후1시', '13:00')):
            source = f'접수기간: 2026.8.25. {start} ~ 8.25. {end}'
            with self.subTest(source=source):
                self.assertTrue(deadline_supported('2026-08-25 ' + expected, source))
        for start, end in (('오후1시', '오전11시'), ('오후11시', '오전12시'),
                           ('오후12시', '오전12시')):
            source = f'접수기간: 2026.8.25. {start} ~ 8.25. {end}'
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-08-25', source))
                self.assertFalse(deadline_supported('2026-08-26 00:00', source))

    def test_missing_year_date_and_relative_endpoints_are_not_repaired(self):
        for source in ('2026년 안내\n신청기간: 8.4.오전10시~8.25.오후3시',
                       '신청기간: 8.4.오전10시~2026.8.25.오후3시',
                       '신청기간: 2026.8.25.오전10시~오후3시',
                       '신청기간: 2026.8.4.오전10시~25일오후3시',
                       '신청기간: 2026.8.4.오전10시~매 교육일 오후3시',
                       '신청기간: 2026.12.31.오전10시~1.1.오후3시',
                       '신청기간: 2026.8.4.오전10시~2.30.오후3시'):
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-08-25', source))
                self.assertFalse(deadline_supported('2026-08-25 15:00', source))

    def test_event_loan_and_announcement_dates_do_not_supply_deadlines(self):
        for label in ('행사기간', '대출기간', '이용기간', '선정발표', '게시일'):
            source = label + ': 2026.8.4.오전10시~2026.8.25.오후3시'
            with self.subTest(label=label):
                self.assertFalse(deadline_supported('2026-08-25', source))
                self.assertFalse(deadline_supported('2026-08-25 15:00', source))
        source = PERIOD + '\n대출기간: 2026.9.1.오전10시~2026.12.31.오후6시'
        self.assertTrue(deadline_supported('2026-08-25', source))
        self.assertFalse(deadline_supported('2026-12-31 18:00', source))

    def test_separate_clauses_and_lines_cannot_supply_end_clock(self):
        for other in ('\n문의시간: 오후3시', '; 행사일: 2026.8.25.오후3시',
                      '\n오후3시까지'):
            source = PERIOD + other
            with self.subTest(source=source):
                self.assertTrue(deadline_supported('2026-08-25', source))
                self.assertFalse(deadline_supported('2026-08-25 15:00', source))
        self.assertFalse(deadline_supported('2026-08-25 15:00',
            '접수 마감: 2026.8.25. 오후3시 행사 시작'))

    def test_distinct_sessions_routes_and_clock_precisions_still_require_review(self):
        period = PERIOD + ' 오후3시'
        for other in ('2회차 접수 마감: 2026.8.26. 오후3시',
                      '방문 접수 마감: 2026.8.25. 오전11시',
                      '온라인 신청 마감: 2026.8.25.',
                      '추가 신청기간: 2026.8.26. 오전10시 ~ 8.27. 오후3시',
                      '추가 신청기간: 2026.8.26. 오전13시 ~ 8.27. 오후3시',
                      '추가 접수기간: 추후 공지', '신청기간: 각 교육일 7일 전까지',
                      '행사 당일 현장 접수 가능'):
            for source in (period + '\n' + other, other + '\n' + period):
                with self.subTest(source=source):
                    self.assertFalse(deadline_supported('2026-08-25', source))
                    self.assertFalse(deadline_supported('2026-08-25 15:00', source))

    def test_same_endpoint_in_twenty_four_hour_notation_is_not_a_second_route(self):
        source = PERIOD + ' 오후3시\n접수 마감: 2026-08-25 15:00'
        self.assertTrue(deadline_supported('2026-08-25 15:00', source))
        self.assertTrue(deadline_supported('2026-08-25', source))

    def test_negated_old_or_explicitly_closed_application_is_not_approved(self):
        period = PERIOD + ' 오후3시'
        for source in (period + ' 아님', period + '\n아님', '변경 전 ' + period,
                       '제외 대상\n' + period, period + '\n신청 불가',
                       period + '\n현재 접수가 마감되었습니다.',
                       period + '\n접수 상태: 마감'):
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-08-25 15:00', source))
                self.assertFalse(deadline_supported('2026-08-25', source))

    def test_conditional_closure_and_denied_onsite_route_do_not_hide_one_period(self):
        for other in ('선착순 마감', '신청은 선착순으로 조기 마감될 수 있습니다.',
                      '현장 접수 불가', '현장 접수 가능하지 않습니다.',
                      '현장 접수 가능\n아님', '접수 마감 아님'):
            with self.subTest(other=other):
                self.assertTrue(deadline_supported('2026-08-25', PERIOD + '\n' + other))

    def test_safety_keeps_grounded_date_but_does_not_clear_attachment_hold(self):
        from services import page_coverage
        from services.news_analysis_safety import assess
        from services.news_profile import NewsProfile

        source = '개인이 신청합니다.\n' + PERIOD
        facts = dict(is_relevant=True, category='복지', target='개인', residence=[],
                     life_cycle=[], target_characteristics=[], interest_topics=[],
                     start_age_months=None, end_age_months=None, evidence_snippet='',
                     residence_evidence=[], life_cycle_evidence=[], applicant_type='individual',
                     applicant_evidence='개인이 신청합니다.', support_detail=source,
                     eligibility_detail='', contact='', deadline='2026-08-25')
        captured = dict(content=source, source_page=page_coverage.make('attachment_review'))
        # Simulate the code-owned whole-source materialization contract, not
        # model-supplied provenance. The page remains attachment-review below.
        facts['fact_evidence'] = {'support_detail': source}
        facts['source_support_selection'] = dict(version=2,
            source_sha256=hashlib.sha256(source.encode('utf-8')).hexdigest(), unit_ids=[1])
        before = copy.deepcopy((facts, captured))
        result = assess(NewsProfile('p', 1, '합성 검사', '전국'), facts, source,
                        whole_document_rules=True, source_payload=captured)
        self.assertEqual(result['analysis']['deadline'], '2026-08-25')
        self.assertNotIn('deadline', result['analysis']['unverified_facts'])
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertIn('attachment_review', result['analysis']['source_page']['reasons'])
        self.assertEqual((facts, captured), before)
        missing_provenance = copy.deepcopy(facts)
        del missing_provenance['source_support_selection']
        held = assess(NewsProfile('p', 1, '합성 검사', '전국'), missing_provenance, source,
                      whole_document_rules=True, source_payload=captured)
        self.assertEqual(held['analysis']['support_detail'], '')
        self.assertEqual(held['analysis']['unverified_facts']['support_detail'], source)
        self.assertEqual(held['analysis']['deadline'], '2026-08-25')
        self.assertFalse(held['accepted'])

    def test_frozen_gijang_source_retains_only_printed_application_day(self):
        path = (Path(__file__).resolve().parents[2] / 'artifacts' /
                'ai-release-fixes-20260914' / 'sources-agent' / 'news-gijang-home-library.txt')
        original = path.read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         '179fdde6c88f427708c17b78b6b9d8b6e0a0e26ec5a5d18890e556fa4737606e')
        source = original.decode('utf-8')
        self.assertEqual(len(source), 557)
        self.assertIn('2026. 8. 4.(화) 오전 10시 ~ 8. 25.(화)', source)
        self.assertTrue(deadline_supported('2026-08-25', source))
        for value in ('2026-08-25 00:00', '2026-08-25 10:00', '2026-08-25 23:59',
                      '2026-08-04 10:00', '2026-08-28', '2026-07-31',
                      '2026-09-01', '2026-12-31'):
            with self.subTest(value=value):
                self.assertFalse(deadline_supported(value, source))
        self.assertEqual(path.read_bytes(), original)

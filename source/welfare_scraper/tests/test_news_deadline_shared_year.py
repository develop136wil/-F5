"""Offline, narrowly scoped shared-year application endpoint regressions.

The immutable real source below is replayed, not fetched or sent to any model.
Synthetic challenges ensure a shorter printed date cannot invent an endpoint.
"""
import hashlib
from pathlib import Path
import unittest

from services.news_deadline_grounding import deadline_supported


class TestNewsDeadlineSharedYear(unittest.TestCase):
    def test_literal_month_day_in_the_same_named_application_period(self):
        cases = (
            ('모집기간: 2026. 8. 24.(월) 10시 ~ 9. 3.(목) 16시', '2026-09-03 16:00'),
            ('모집기간\n: 2026. 8. 24.(월) 10시 ~ 9. 3.(목) 16시', '2026-09-03 16:00'),
            ('접수기간: 2026년 8월 24일 ~ 9월 3일 16시 30분까지', '2026-09-03 16:30'),
            ('신청기간: 2026/08/24 10:00 ~ 09/03 16:30', '2026-09-03T16:30'),
            ('접수기간: 2026-09-03 10:00 ~ 09-03 16:30', '2026-09-03 16:30'),
            ('신청기간: 2026.8.24.~9.3.', '2026-09-03'),
            ('접수기간: 2026년 8월 24일부터 9월 3일까지', '2026-09-03'),
            ('모집기간: 2024.2.1.~2.29. 00:00', '2024-02-29 00:00'),
        )
        for source, value in cases:
            with self.subTest(source=source, value=value):
                self.assertTrue(deadline_supported(value, source))

    def test_year_is_not_borrowed_from_title_other_line_or_other_endpoint(self):
        cases = (
            ('2026년 행사\n모집기간: 8.24.~9.3.16시', '2026-09-03 16:00'),
            ('2026년 모집\n접수 마감: 9.3.16시', '2026-09-03 16:00'),
            ('신청기간: 8.24.~2026.9.3.16시', '2026-09-03 16:00'),
            ('신청기간: 2026.8.24.부터입니다. 행사일은 9.3.16시', '2026-09-03 16:00'),
            ('접수기간: 2026.8.24.~3일 16시', '2026-08-03 16:00'),
            ('접수기간: 2026.8.24.~9월 16시', '2026-09-16'),
            ('접수기간: 2026.8.24.~9월 16분', '2026-09-16'),
            ('접수기간: 2026.8.24.~9.3.2026', '2026-09-03'),
        )
        for source, value in cases:
            with self.subTest(source=source, value=value):
                self.assertFalse(deadline_supported(value, source))

    def test_no_year_rollover_or_reversed_calendar_or_clock(self):
        for source, values in (
            ('모집기간: 2026.12.20.~1.3.16시', ('2026-01-03', '2027-01-03 16:00')),
            ('모집기간: 2026.9.4.~9.3.16시', ('2026-09-03', '2026-09-03 16:00')),
            ('모집기간: 2026.9.3.18시~9.3.16시', ('2026-09-03', '2026-09-03 16:00')),
        ):
            for value in values:
                with self.subTest(source=source, value=value):
                    self.assertFalse(deadline_supported(value, source))

    def test_clock_is_bound_to_the_second_endpoint_only(self):
        for source in (
            '모집기간: 2026.8.24.10시~9.3.',
            '모집기간: 2026.8.24.~9.3.\n문의시간 16시',
            '모집기간: 2026.8.24.~9.3.; 행사일 2026.9.3.16시',
        ):
            with self.subTest(source=source):
                self.assertTrue(deadline_supported('2026-09-03', source))
                for clock in ('00:00', '10:00', '16:00'):
                    self.assertFalse(deadline_supported('2026-09-03 ' + clock, source))

    def test_malformed_source_clock_cannot_fall_back_to_date_only(self):
        for clock in ('24시', '25:00', '16:60', '16:30:00', '16시 60분',
                      '16시 30분 15초', '16시 30', 'T16', '16:',
                      '16:30+09:00', '16:30Z'):
            source = '모집기간: 2026.8.24.~9.3. ' + clock
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-09-03', source))
                self.assertFalse(deadline_supported('2026-09-03 16:30', source))
        for endpoint in ('2.30.16시', '13.3.16시', '9.0.16시'):
            source = '모집기간: 2026.1.1.~' + endpoint
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-09-03', source))

    def test_non_application_and_untyped_range_remain_unverified(self):
        for label in ('행사기간', '게시기간', '강의일시', '신청일시', '신청', '접수', '신청마감'):
            source = label + ': 2026.8.24.10시~9.3.16시'
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-09-03 16:00', source))
                self.assertFalse(deadline_supported('2026-09-03', source))

    def test_old_cancelled_excluded_and_wrapped_rejection_remain_unverified(self):
        period = '모집기간: 2026.8.24.10시~9.3.16시'
        sources = (
            period + ' 아님', period + '\n아님', period + '; 취소',
            '모집기간\n: 2026.8.24.10시~9.3.16시\n아님',
            '변경 전 ' + period, '기존 ' + period, '종전 ' + period,
            '제외 대상 ' + period, '모집기간 아님: 2026.8.24.~9.3.16시',
            '변경 전\n' + period, '취소\n' + period, '제외 대상\n' + period,
            period + '\n제외', period + '\n신청 불가', period + '\n접수 금지',
            '제외 조건:\n' + period, '신청 불가\n' + period,
            period + ' 지원하지 않음', period + '\n지원하지 않음',
            '모집기간\n: 2026.8.24.10시~9.3.16시\n접수 금지',
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(deadline_supported('2026-09-03 16:00', source))
                self.assertFalse(deadline_supported('2026-09-03', source))

    def test_short_period_does_not_choose_between_other_application_routes(self):
        period = '모집기간: 2026.8.24.10시~9.3.16시'
        for other in (
            '현장 접수 가능', '행사 당일 현장 접수 가능', '현장에서 접수도 가능',
            '추가 접수기간: 2026.9.4.~9.5.16시',
            '온라인 접수 마감: 2026.9.2.16시',
            '사전신청: 2026.9.2.16시까지',
            '추가 접수기간: 9.4.~9.5.',
            '추가 접수기간\n: 9.4.~9.5.',
            '추가 접수기간: 9월 4일~5일',
            '별도 접수 마감: 9.5.16시',
            '추가 접수기간: 2026.9.4.~9월 16시',
            '추가 접수기간: 2026.9.4.~9.5.25시',
        ):
            with self.subTest(other=other):
                self.assertFalse(deadline_supported('2026-09-03 16:00', period + '\n' + other))
                self.assertFalse(deadline_supported('2026-09-03 16:00', other + '\n' + period))
        self.assertTrue(deadline_supported('2026-09-03 16:00', period +
                                          '\n신청방법: 온라인 신청(선착순 마감)'))

    def test_complete_year_existing_contract_is_preserved(self):
        self.assertTrue(deadline_supported('2027-01-03 16:00',
            '접수기간: 2026.12.20.~2027.1.3.16시'))
        self.assertTrue(deadline_supported('2026-09-03 16:00',
            '접수마감: 2026.9.3.16시'))

    def test_frozen_real_source_supports_only_printed_application_endpoint(self):
        path = (Path(__file__).resolve().parents[2] / 'artifacts' /
                'ai-fresh-eval-20260910' / 'fixtures' / 'news-seoul-wall.txt')
        original_bytes = path.read_bytes()
        self.assertEqual(hashlib.sha256(original_bytes).hexdigest(),
                         '312e6c7c4e9d35ec309c0cff5c40e0c6bf291fbd3b92d17831f481d6ee3e41a3')
        source = original_bytes.decode('utf-8')
        self.assertTrue(deadline_supported('2026-09-03 16:00', source))
        self.assertTrue(deadline_supported('2026-09-03', source))
        for wrong in ('2025-09-03 16:00', '2027-09-03 16:00', '2026-09-03 00:00',
                      '2026-09-03 10:00', '2026-09-11 16:00', '2026-10-02 16:00'):
            with self.subTest(wrong=wrong):
                self.assertFalse(deadline_supported(wrong, source))
        self.assertEqual(path.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()

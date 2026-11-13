"""Exact title-span presentation only; no model calls or independent OCR claims."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from services import ocr_service
from tests.test_ocr_evidence import candidate


class TestOcrTitleReview(unittest.TestCase):
    def evaluate(self, source, title, quotes, **fields):
        raw = candidate(source, title=title, **fields)
        raw['field_evidence']['title'] = quotes
        before = deepcopy(raw)
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(raw, before)
        return result

    def assert_held(self, source, title, quotes):
        result = self.evaluate(source, title, quotes)
        self.assertEqual(result['title'], '')
        self.assertEqual(result['_ocr_review']['unverified_fields']['title'], title)
        self.assertEqual(result['support_detail'], source)
        self.assertTrue(result['needs_source_review'])

    def test_consecutive_whole_title_lines_allow_only_whitespace_presentation(self):
        source = '2026 하반기 어린이 전집 대출\n우리집 도서관\n모집대상: 10가족'
        quotes = source.splitlines()[:2]
        for title in ('2026 하반기 어린이 전집 대출 우리집 도서관',
                      ' 2026  하반기 어린이 전집 대출\t우리집 도서관 ',
                      '2026 하반기 어린이 전집 대출\n우리집 도서관'):
            with self.subTest(title=title):
                result = self.evaluate(source, title, quotes)
                self.assertEqual(result['title'], title)  # Never rewrite model output.
                self.assertNotIn('title', result['_ocr_review']['unverified_fields'])
                self.assertEqual(result['support_detail'], source)
                self.assertTrue(result['needs_source_review'])
                self.assertIs(result['_ocr_review']['transcription_independently_verified'], False)

    def test_one_multiline_quote_or_blank_line_between_whole_quotes_can_match(self):
        source = '어린이 전집 대출\n\n우리집 도서관\n모집 안내'
        for quotes in (['어린이 전집 대출\n\n우리집 도서관'],
                       ['어린이 전집 대출', '우리집 도서관']):
            with self.subTest(quotes=quotes):
                result = self.evaluate(source, '어린이 전집 대출 우리집 도서관', quotes)
                self.assertEqual(result['title'], '어린이 전집 대출 우리집 도서관')

    def test_word_boundaries_and_punctuation_cannot_be_added_or_removed(self):
        source = '공공 지원\n우리집도서관: 10가족'
        for title in ('공공지원 우리집도서관: 10가족',
                      '공공 지원 우리집 도서관: 10가족',
                      '공공 지원 우리집도서관 10가족',
                      '공공 지원 우리집도서관: 100가족'):
            with self.subTest(title=title):
                self.assert_held(source, title, source.splitlines())

    def test_added_removed_or_reordered_words_remain_unverified(self):
        source = '2026 어린이 전집 대출\n우리집 도서관 모집 취소'
        for title in ('2026 어린이 무료 전집 대출 우리집 도서관 모집 취소',
                      '2026 어린이 대출 우리집 도서관 모집 취소',
                      '2026 어린이 전집 대출 우리집 도서관 모집',
                      '우리집 도서관 모집 취소 2026 어린이 전집 대출'):
            with self.subTest(title=title):
                self.assert_held(source, title, source.splitlines())

    def test_fragment_quote_and_omitting_a_selected_subtitle_are_not_full_titles(self):
        source = '2026 어린이 전집 대출\n우리집 도서관 모집 취소'
        self.assert_held(source, '어린이 전집 대출 우리집 도서관',
                         ['어린이 전집 대출', '우리집 도서관'])
        for title in source.splitlines():
            with self.subTest(title=title):
                self.assert_held(source, title, source.splitlines())

    def test_partial_title_identifier_cannot_drop_a_predicate_in_a_full_quote(self):
        source = '우리집 도서관 모집 취소'
        self.assert_held(source, '우리집 도서관', [source])
        # Deliberately conservative: omitting a printed title label is not a
        # whitespace-only change. No automatic label parser is introduced.
        self.assert_held('제목: 우리집 도서관', '우리집 도서관', ['제목: 우리집 도서관'])

    def test_nonconsecutive_evidence_cannot_hide_an_intervening_condition(self):
        source = '어린이 전집 대출\n올해 모집은 취소되었습니다.\n우리집 도서관'
        self.assert_held(source, '어린이 전집 대출 우리집 도서관',
                         ['어린이 전집 대출', '우리집 도서관'])

    def test_reordered_or_ambiguous_duplicate_spans_stay_on_review(self):
        source = '어린이 전집 대출\n우리집 도서관'
        self.assert_held(source, '우리집 도서관 어린이 전집 대출',
                         ['우리집 도서관', '어린이 전집 대출'])
        duplicate = source + '\n별도 안내\n' + source
        self.assert_held(duplicate, '어린이 전집 대출 우리집 도서관', source.splitlines())
        self.assert_held('지원 안내\n지원 안내\n지원 안내',
                         '지원 안내 지원 안내', ['지원 안내', '지원 안내'])

    def test_nonordinary_whitespace_and_zero_width_characters_are_not_erased(self):
        for separator in ('\u200b', '\u00a0', '\u2028', '\x0b'):
            with self.subTest(separator=repr(separator)):
                source = '우리집' + separator + '도서관'
                self.assert_held(source, '우리집 도서관', [source])

    def test_partial_transcription_does_not_become_complete_or_skip_human_review(self):
        source = '어린이 전집 대출\n우리집 도서관'
        result = self.evaluate(source, '어린이 전집 대출 우리집 도서관',
                               source.splitlines(), source_complete=False)
        self.assertEqual(result['title'], '어린이 전집 대출 우리집 도서관')
        self.assertTrue(result['needs_source_review'])
        self.assertIs(result['_ocr_review']['source_complete'], False)
        self.assertIn('ocr_transcription_partial', result['_ocr_review']['reasons'])
        for change in ({'source_text': ''}, {'source_complete': 'true'}, {'field_evidence': {}}):
            with self.subTest(change=change):
                raw = candidate(source, title='어린이 전집 대출 우리집 도서관')
                raw['field_evidence']['title'] = source.splitlines()
                raw.update(change)
                held = ocr_service.validate_ocr_result(raw)
                self.assertEqual(held['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
                self.assertFalse(held['is_relevant'])

    def test_title_whitespace_rule_does_not_relax_contact_or_eligibility(self):
        source = '우리집 도서관\n기장군 거주\n가족 회원 가입\n문의: 709-5322'
        raw = candidate(source, title='우리집  도서관', contact='051-709-5322',
                        eligibility_detail='기장군 거주 가족 회원 가입')
        raw['field_evidence']['title'] = ['우리집 도서관']
        raw['field_evidence']['contact'] = ['문의: 709-5322']
        raw['field_evidence']['eligibility_detail'] = ['기장군 거주', '가족 회원 가입']
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['title'], raw['title'])
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['eligibility_detail'], '')
        self.assertEqual(result['support_detail'], source)
        self.assertEqual(result['_ocr_review']['unverified_fields']['contact'], raw['contact'])
        self.assertEqual(result['_ocr_review']['unverified_fields']['eligibility_detail'], raw['eligibility_detail'])

    def test_frozen_public_response_title_is_kept_without_rescuing_dates_or_other_fields(self):
        from tests.test_ocr_date_review import frozen_response
        raw, _ = frozen_response()
        before = deepcopy(raw)
        with patch.object(ocr_service, 'get_client', side_effect=AssertionError('No API client')) as client:
            result = ocr_service.validate_ocr_result(raw)
        client.assert_not_called()
        self.assertEqual(result['title'], raw['title'])
        self.assertNotIn('title', result['_ocr_review']['unverified_fields'])
        self.assertEqual(result['support_detail'], raw['source_text'])
        self.assertEqual(result['_ocr_review']['source_text'], raw['source_text'])
        self.assertTrue(result['needs_source_review'])
        self.assertIs(result['_ocr_review']['transcription_independently_verified'], False)
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['life_cycle'], [])
        self.assertEqual(result['target_characteristics'], [])
        self.assertEqual(result['residence'], ['기장군'])
        for key in ('deadline', 'event_date', 'event_end_date'):
            self.assertIsNone(result[key])
            self.assertEqual(result['_ocr_review']['unverified_fields'][key], raw[key])
        self.assertEqual(raw, before)

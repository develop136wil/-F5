import os
import sys
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.policy_evidence import validate_policy_evidence
import ai_classifier


class EvidenceTests(unittest.TestCase):
    def test_labelled_benefits_cannot_exchange_amounts(self):
        quote = '일반주차장: 1시간 면제 후 50% 할인. 환승주차장: 3시간 면제 후 80% 할인.'
        for value in ['일반주차장: 1시간 면제 후 80% 할인. 환승주차장: 3시간 면제 후 50% 할인.',
                      '일반주차장: 3시간 면제 후 50% 할인. 환승주차장: 1시간 면제 후 80% 할인.']:
            self.assertFalse(validate_policy_evidence({'support_detail': value,
                '_source_evidence': {'support_detail': quote}}, quote))
        value = '환승주차장: 3시간 면제 후 80% 감면. 일반주차장: 1시간 면제 후 50% 감면.'
        self.assertTrue(validate_policy_evidence({'support_detail': value,
            '_source_evidence': {'support_detail': quote}}, quote))

    def test_generic_labels_and_decimal_values(self):
        quote = '청년: 1.5만원 지원\n노년: 2.5만원 지원'
        swapped = '청년: 2.5만원 지원\n노년: 1.5만원 지원'
        self.assertTrue(validate_policy_evidence({'support_detail': quote,
            '_source_evidence': {'support_detail': quote}}, quote))
        self.assertFalse(validate_policy_evidence({'support_detail': swapped,
            '_source_evidence': {'support_detail': quote}}, quote))

    def test_grounded_discount(self):
        result = {'support_detail': '주차요금 50% 할인', '_source_evidence': {'support_detail': '공영주차장 요금 50% 할인'}}
        # The shortened paraphrase drops "public parking" scope. Preserve words
        # instead of treating the same numeric amount as semantic proof.
        self.assertFalse(validate_policy_evidence(result, '안내\n공영주차장 요금 50% 할인\n문의'))
        result['support_detail'] = '공영주차장 요금 50% 할인'
        self.assertTrue(validate_policy_evidence(result, '안내\n공영주차장 요금 50% 할인\n문의'))

    def test_invented_quote_or_amount_is_rejected(self):
        result = {'support_detail': '50% 할인', '_source_evidence': {'support_detail': '공영주차장 50% 할인'}}
        self.assertFalse(validate_policy_evidence(result, '박물관 무료 입장'))
        result['_source_evidence']['support_detail'] = '공영주차장 20% 할인'
        self.assertFalse(validate_policy_evidence(result, '공영주차장 20% 할인'))

    def test_missing_or_invalid_evidence(self):
        for evidence in [None, {}, [], {'support_detail': None}]:
            self.assertFalse(validate_policy_evidence({'support_detail': '주차요금 할인', '_source_evidence': evidence}, '주차요금 할인'))

    def test_missing_discount_or_time_is_rejected(self):
        quote = '일반주차장 1시간 면제 후 50% 할인, 환승주차장 3시간 면제 후 80% 할인'
        for value in ['일반주차장 1시간 면제 후 50% 할인',
                      '일반주차장 50% 할인, 환승주차장 80% 할인',
                      '일반주차장 1시간 면제 후 50% 할인, 환승주차장 3시간 면제 후 50% 할인']:
            self.assertFalse(validate_policy_evidence({'support_detail': value,
                '_source_evidence': {'support_detail': quote}}, quote))
        self.assertTrue(validate_policy_evidence({'support_detail': quote,
            '_source_evidence': {'support_detail': quote}}, quote))

    def test_changed_units_are_rejected(self):
        for quote, value in [('지원금 50원 지급', '지원금 50% 지급'),
                             ('생후 6개월까지', '만 6세까지')]:
            self.assertFalse(validate_policy_evidence({'age_detail': value,
                '_source_evidence': {'age_detail': quote}}, quote))

    def test_commas_spacing_and_percent_word_are_equivalent(self):
        quote = '지원금 1,000원 및 50% 할인'
        self.assertTrue(validate_policy_evidence({'support_detail': '지원금 1000 원 및 50 퍼센트 할인',
            '_source_evidence': {'support_detail': quote}}, quote))

    def test_exact_money_conversion_is_allowed(self):
        quote = '지원금 1만원 지급'
        self.assertTrue(validate_policy_evidence({'support_detail': '지원금 10000원 지급',
            '_source_evidence': {'support_detail': quote}}, quote))

    def test_exact_duration_and_decimal_money(self):
        for quote, value in [('주차 1시간 면제', '주차 60분 면제'),
                             ('지원금 1.5만원 지급', '지원금 15000원 지급')]:
            self.assertTrue(validate_policy_evidence({'support_detail': value,
                '_source_evidence': {'support_detail': quote}}, quote))

    def test_age_and_calendar_conversions_stay_separate(self):
        for quote, value in [('대상 만 1세', '대상 12개월'), ('지원 기간 1월', '지원 기간 30일'),
                             ('지원금 1.5만원', '지원금 1500원')]:
            self.assertFalse(validate_policy_evidence({'support_detail': value,
                '_source_evidence': {'support_detail': quote}}, quote))


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_tag_only_responses_fail_without_extra_ai_calls(self):
        for response in [{}, {'category': '복지'}, {'support_detail': ''},
                         {'support_detail': []}, {'support_detail': 'None'},
                         {'support_detail': '   '}]:
            with self.subTest(response=response):
                with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=response)) as ai:
                    result = await ai_classifier.extract_policy_fields_async(object(), '주차 정책',
                        'https://example.invalid', content='주차요금 50% 할인')
                    self.assertIsNone(result)
                    self.assertEqual(ai.await_count, 1)

    async def test_no_source_never_calls_ai(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            self.assertIsNone(await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content='  '))
            call.assert_not_awaited()

    async def test_ungrounded_response_is_failed_not_empty_fields(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={'support_detail': '박물관 무료'})):
            self.assertIsNone(await ai_classifier.extract_policy_fields_async(object(), '주차 정책', 'https://example.invalid', content='공영주차장 요금 할인'))

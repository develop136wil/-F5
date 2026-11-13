"""Synthetic one-call workflow. No live SDK, data store or Notion operations."""
import copy
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_source_plan import hydrate_policy_source_ranges
from services.policy_source_units import source_units
from services.policy_field_requirements import policy_field_requirements


SOURCE = ('소득\n소득무관\n지원대상\n등록 여성장애인에게 지원\n지원내용\n출산비용 120만원 지원\n'
          '문의처\n각 동 행정복지센터\n장애인복지과 481-2562')


def response(*, income=True, contact=True):
    requirements = policy_field_requirements(SOURCE)
    return dict(_source_units=dict(support_detail=[u['id'] for u in source_units(SOURCE)],
        eligibility_detail=[], age_detail=[], target_income=sorted({i['unit_id']
            for i in requirements['target_income']}) if income else []),
        category='', target_characteristics=['장애/발달지연'], residence=[], life_cycle=[],
        start_age_months=None, end_age_months=None,
        contact=requirements['contact']['text'] if contact else '장애인복지과 481-2562')


class PolicyFieldWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_supported_target_reminder_is_validated_without_tag_insertion(self):
        source = '지원대상\n등록장애인에게 지원\n지원내용\n교통비 월 3만원 지원'
        for selected in ([], ['장애/발달지연']):
            raw = dict(_source_units=dict(support_detail=[u['id'] for u in source_units(source)],
                eligibility_detail=[], age_detail=[], target_income=[]), category='',
                target_characteristics=selected, residence=[], life_cycle=[],
                start_age_months=None, end_age_months=None, contact='')
            before, review = copy.deepcopy(raw), {}
            with self.subTest(selected=selected), patch.object(ai_classifier, '_call_gemini_json',
                    new=AsyncMock(return_value=raw)) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                    'https://example.invalid', content=source, review=review)
            call.assert_awaited_once()
            self.assertEqual(raw, before)
            prompt = call.call_args.args[1]
            self.assertTrue('[Source-backed target reminders JSON]' in prompt or 'Target reminders:' in prompt)
            self.assertTrue('at least ONE exact offered label' in prompt or 'ONE supported OR-label' in prompt)
            if selected:
                self.assertIsNotNone(result)
                self.assertEqual(review, {})
            else:
                self.assertIsNone(result)
                self.assertEqual(review['code'], 'source_field_incomplete')
                self.assertEqual(review['fields'], ['target_characteristics'])
                self.assertIn('대상 검색 분류', review['note'])

    async def test_complete_synthetic_response_returns_once_without_repair(self):
        raw = response()
        before, review = copy.deepcopy(raw), {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        call.assert_awaited_once()
        self.assertIsNotNone(result)
        self.assertEqual(review, {})
        self.assertEqual(raw, before)
        self.assertIn('소득무관', result['target_income'])
        self.assertEqual(result['contact'], '각 동 행정복지센터\n장애인복지과 481-2562')

    async def test_union_coverage_does_not_hide_per_field_omissions_or_trigger_another_call(self):
        for income, contact, expected in ((False, True, ['target_income']),
                (True, False, ['contact']), (False, False, ['contact', 'target_income'])):
            raw, review = response(income=income, contact=contact), {}
            before = copy.deepcopy(raw)
            with self.subTest(income=income, contact=contact), patch.object(ai_classifier,
                    '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                    'https://example.invalid', content=SOURCE, review=review)
            call.assert_awaited_once()
            self.assertIsNone(result)
            self.assertEqual(raw, before)
            self.assertEqual(review['code'], 'source_field_incomplete')
            self.assertEqual(review['fields'], expected)
            self.assertIn('해당 전용 항목', review['note'])

    async def test_legacy_evidence_contract_is_not_silently_migrated_or_reclassified(self):
        legacy = hydrate_policy_source_ranges(response(income=False, contact=False), SOURCE)
        legacy.pop('_source_unit_selection')
        before, review = copy.deepcopy(legacy), {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=legacy)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        call.assert_awaited_once()
        self.assertIsNotNone(result)
        self.assertEqual(legacy, before)
        self.assertEqual(result['target_income'], '')
        self.assertEqual(result['contact'], '장애인복지과 481-2562')
        self.assertEqual(review, {})

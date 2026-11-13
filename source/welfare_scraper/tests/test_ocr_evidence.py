"""Model-transcription consistency only, not independent image accuracy."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from services import ocr_evidence, ocr_service


def candidate(source='교육비 지원', **fields):
    value = {key: '' for key in ocr_evidence.TEXT_FIELDS}
    value.update({key: [] for key in ocr_evidence.LIST_FIELDS})
    value.update({key: None for key in (*ocr_evidence.DATE_FIELDS, *ocr_evidence.AGE_FIELDS)})
    value.update(is_relevant=True, source_text=source, source_complete=True,
                 field_evidence={key: [] for key in ocr_evidence.EVIDENCE_FIELDS})
    value.update(fields)
    return value


class TestOcrEvidence(unittest.TestCase):
    def test_schema_requests_transcription_once_and_no_cost_summary_template(self):
        schema = ocr_service.response_schema()
        self.assertTrue({'source_text', 'source_complete', 'field_evidence'} <= set(schema['required']))
        self.assertEqual(schema['properties']['support_detail'], {'type': 'string', 'enum': ['']})
        evidence = schema['properties']['field_evidence']
        self.assertEqual(set(evidence['required']), set(ocr_evidence.EVIDENCE_FIELDS))
        self.assertFalse(evidence['additionalProperties'])

    def test_exact_transcription_and_phone_remain_manual_review_candidates(self):
        source = '교육비 지원\n문의: 709-5322'
        raw = candidate(source, title='교육비 지원', contact='709-5322')
        raw['field_evidence']['title'] = ['교육비 지원']
        raw['field_evidence']['contact'] = ['문의: 709-5322']
        before = deepcopy(raw)
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['support_detail'], source)
        self.assertEqual(result['title'], '교육비 지원')
        self.assertEqual(result['contact'], '709-5322')
        self.assertTrue(result['needs_source_review'])
        self.assertIs(result['_ocr_review']['required'], True)
        self.assertIs(result['_ocr_review']['transcription_independently_verified'], False)
        self.assertIs(result['_ocr_review']['source_complete'], True)
        self.assertEqual(result['_ocr_review']['source_text'], source)
        self.assertEqual(raw, before)

    def test_invented_free_and_expanded_phone_are_removed_but_candidates_preserved(self):
        source = '전집 대출\n문의: 709-5322'
        raw = candidate(source, support_detail='전집 대출\n비용: 무료', contact='051-709-5322')
        raw['field_evidence']['contact'] = ['문의: 709-5322']
        raw['field_evidence']['support_detail'] = ['전집 대출']
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['support_detail'], source)
        self.assertNotIn('무료', result['support_detail'])
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['_ocr_review']['unverified_fields']['contact'], '051-709-5322')
        self.assertIn('무료', result['_ocr_review']['unverified_fields']['support_detail'])

    def test_fragment_cannot_turn_negated_cost_into_affirmative_metadata(self):
        for quote in ('무료', '참가비: 무료 아님'):
            with self.subTest(quote=quote):
                raw = candidate('참가비: 무료 아님', eligibility_detail='무료')
                raw['field_evidence']['eligibility_detail'] = [quote]
                result = ocr_service.validate_ocr_result(raw)
                self.assertEqual(result['eligibility_detail'], '')
                self.assertEqual(result['support_detail'], '참가비: 무료 아님')

    def test_fabricated_quote_is_not_source_evidence(self):
        raw = candidate('교육비 지원', contact='02-1234-5678')
        raw['field_evidence']['contact'] = ['문의: 02-1234-5678']
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['_ocr_review']['unverified_fields']['contact'], '02-1234-5678')

    def test_partial_source_keeps_editable_text_without_becoming_complete(self):
        raw = candidate('읽을 수 있는 지원 내용', source_complete=False)
        result = ocr_service.validate_ocr_result(raw)
        self.assertTrue(result['is_relevant'])
        self.assertEqual(result['support_detail'], raw['source_text'])
        self.assertNotIn('error_code', result)
        self.assertIn('ocr_transcription_partial', result['_ocr_review']['reasons'])
        self.assertIs(result['_ocr_review']['source_complete'], False)
        self.assertTrue(result['needs_source_review'])

    def test_missing_transcription_or_invalid_contract_is_static_review_hold(self):
        for change in ({'source_text': ''}, {'source_text': None}, {'source_complete': 'true'},
                       {'field_evidence': {}}, {'field_evidence': []}):
            with self.subTest(change=change):
                result = ocr_service.validate_ocr_result(candidate(**change))
                self.assertEqual(result['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
                self.assertFalse(result['is_relevant'])
                self.assertTrue(result['needs_source_review'])
                self.assertTrue(result['_ocr_review']['required'])
                self.assertIs(result['_ocr_review']['source_complete'], False)

    def test_explicit_year_age_converts_using_existing_guard_not_guessed_birth(self):
        source = '지원대상: 만 2세 미만 영아\n교육비 지원'
        raw = candidate(source, end_age_months=23, life_cycle=['영아'])
        raw['field_evidence']['end_age_months'] = [source.splitlines()[0]]
        raw['field_evidence']['life_cycle'] = [source.splitlines()[0]]
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['end_age_months'], 23)
        self.assertIsNone(result['start_age_months'])
        self.assertEqual(result['life_cycle'], ['영아'])
        self.assertTrue(result['needs_source_review'])
        raw['start_age_months'] = 0
        raw['field_evidence']['start_age_months'] = [source.splitlines()[0]]
        self.assertIsNone(ocr_service.validate_ocr_result(raw)['start_age_months'])

    def test_provider_guardian_and_duration_are_not_beneficiary_ages(self):
        for source in ('보호자: 만 18세 이상\n지원내용: 아동 교육비 지원',
                       '제공인력: 만 18세 이상\n지원내용: 교육 지원',
                       '지원기간: 18개월\n지원내용: 교육비 지원'):
            with self.subTest(source=source):
                raw = candidate(source, start_age_months=216, life_cycle=['청소년'])
                raw['field_evidence']['start_age_months'] = [source.splitlines()[0]]
                raw['field_evidence']['life_cycle'] = [source.splitlines()[0]]
                result = ocr_service.validate_ocr_result(raw)
                self.assertIsNone(result['start_age_months'])
                self.assertEqual(result['life_cycle'], [])

    def test_venue_labels_and_missing_income_are_not_recipient_metadata(self):
        source = '문의: 서울시 유아자료실\n전집 대출'
        raw = candidate(source, target_income='소득 무관', residence=['서울시'],
                        life_cycle=['영아', '유아', '학령기', '청소년'], target_characteristics=['일반'])
        for key in ('target_income', 'residence', 'life_cycle', 'target_characteristics'):
            raw['field_evidence'][key] = [source.splitlines()[0]]
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['target_income'], '')
        for key in ('residence', 'life_cycle', 'target_characteristics'):
            self.assertEqual(result[key], [])

    def test_application_period_is_not_event_period(self):
        source = '접수 기간: 2026-08-04 ~ 2026-08-25\n전집 대출'
        raw = candidate(source, deadline='2026-08-25', event_date='2026-08-04', event_end_date='2026-08-25')
        for key in ocr_evidence.DATE_FIELDS:
            raw['field_evidence'][key] = [source.splitlines()[0]]
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['deadline'], '2026-08-25')
        self.assertIsNone(result['event_date'])
        self.assertIsNone(result['event_end_date'])
        self.assertEqual(result['support_detail'], source)

    def test_explicit_use_period_can_stay_but_multiple_or_unstated_year_cannot(self):
        source = '이용기간: 2026-09-01 ~ 2026-12-31'
        raw = candidate(source, event_date='2026-09-01', event_end_date='2026-12-31')
        for key in ('event_date', 'event_end_date'):
            raw['field_evidence'][key] = [source]
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual((result['event_date'], result['event_end_date']), ('2026-09-01', '2026-12-31'))
        for changed in (source + '\n이용기간: 2027-01-01 ~ 2027-01-31',
                        '2026년 안내\n이용기간: 9월 1일 ~ 12월 31일'):
            with self.subTest(source=changed):
                raw['source_text'] = changed
                for key in ('event_date', 'event_end_date'):
                    raw['field_evidence'][key] = [changed.splitlines()[-1]]
                result = ocr_service.validate_ocr_result(raw)
                self.assertIsNone(result['event_date'])
                self.assertIsNone(result['event_end_date'])

    def test_request_metadata_url_is_not_visibly_printed_evidence(self):
        raw = candidate('전집 대출', related_urls=['https://example.invalid/download'])
        raw['field_evidence']['related_urls'] = ['전집 대출']
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['related_urls'], [])

    def test_frozen_previous_live_response_without_transcription_is_not_grandfathered(self):
        path = Path(__file__).resolve().parents[2] / 'artifacts/ai-acceptance-live-20260914/fresh-live-01/ocr-gijang-home-library.sdk-response.json'
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), '9f816627fc7c871d753909eb5d6866c39a0272834a3936a3da248092ff727bc0')
        raw = json.loads(json.loads(data)['candidates'][0]['content']['parts'][0]['text'])
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        self.assertNotIn('support_detail', result)
        self.assertIn('무료', result['_ocr_review']['unverified_fields']['support_detail'])
        self.assertEqual(result['_ocr_review']['unverified_fields']['contact'], '051-709-5322')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(data).hexdigest())

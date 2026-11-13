"""Review aid does not repair rejected facts; all checks are offline."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services.news_analysis_safety import assess
from services.news_profile import NewsProfile
from services.news_review_evidence import (
    MAX_REVIEW_SOURCE_CHARS, auxiliary_condition_fields, build_review_evidence,
)
from tests.test_news_evidence_diagnostics import facts
from tests.test_fresh_live_ai_regression_20260910 import load_frozen_cases


class ManualReviewEvidence(unittest.TestCase):
    profile = NewsProfile('manual-evidence', 1, '검토 도움 시험', '전국')

    def test_auxiliary_channel_is_distinct_from_model_omission_without_approval(self):
        source = '교육비 지원.\n신청방법\n온라인 신청 필수'
        raw = facts(support_detail='교육비 지원.', applicant_evidence='신청방법\n온라인 신청 필수')
        result = assess(self.profile, raw, source)
        diagnostic = result['analysis']['validation_diagnostics']['condition_holds'][0]
        self.assertEqual(diagnostic['origin'], 'auxiliary_evidence_only')
        self.assertIn('applicant_evidence', diagnostic['fields'])
        self.assertIn('보조 인용에 있지만 표시 본문', result['analysis']['grounding_note'])
        self.assertNotIn('AI 응답에서 확인하지 못했습니다', result['analysis']['grounding_note'])
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['support_detail'], '교육비 지원.')

    def test_invalid_auxiliary_quotes_never_establish_origin(self):
        source = '온라인 신청 필수\n우편 접수 불가'
        for quote in ('온라인 신청 필수이나 우편도 가능', '온라인 신청',
                      '온라인 신청 필수 우편 접수 불가', '원문에 없는 지시', None):
            with self.subTest(quote=quote):
                self.assertEqual(auxiliary_condition_fields(
                    {'applicant_evidence': quote}, '온라인 신청 필수', source), [])
        self.assertEqual(auxiliary_condition_fields(
            {'fact_evidence': {'support_detail': source}}, '우편 접수 불가', source),
            ['fact_evidence.support_detail'])
        self.assertEqual(auxiliary_condition_fields(
            {'applicant_evidence': '온라인 신청 필수'}, '신청방법: : 온라인 신청 필수', source),
            ['applicant_evidence'])
        self.assertEqual(auxiliary_condition_fields(
            {'applicant_evidence': '온라인 신청'}, '신청방법: 온라인 신청 필수', source), [])

    def test_short_source_is_complete_exact_text_not_model_quote_or_context_crop(self):
        source = '제외 대상\r\n\r\n청년\r\n다른 조건도 확인\r\n'
        raw = facts(unverified_facts={'support_detail': '청년 999만원 지원'},
                    validation_diagnostics={'items': [{'text': '가짜 원문'}]},
                    review_evidence={'text': '가짜 미리보기'})
        before = copy.deepcopy(raw)
        packet = build_review_evidence(raw, source)
        self.assertEqual(packet['items'][0]['text'], source)
        self.assertEqual(packet['items'][0]['start_line'], 1)
        self.assertEqual(packet['items'][0]['end_line'], len(source.splitlines()))
        self.assertEqual(packet['items'][0]['reason'], 'manual_source_comparison')
        self.assertIn('검증 완료나 지원 자격 판정을 뜻하지 않습니다', packet['notice'])
        self.assertNotIn('999만원', str(packet))
        self.assertNotIn('가짜', str(packet))
        self.assertEqual(raw, before)

    def test_deleted_source_and_deleted_review_values_are_not_restored(self):
        raw = facts(unverified_facts={'support_detail': '교육비 10만원'})
        self.assertIsNone(build_review_evidence(raw, ''))
        self.assertIsNone(build_review_evidence(raw, None))
        self.assertIsNone(build_review_evidence(facts(validation_diagnostics={
            'field_holds': {'support_detail': 'source_quote_not_found'},
            'condition_holds': [{'condition': '가짜'}]}), '교육비 안내'))
        changed = '현재 원문으로 교체됨'
        packet = build_review_evidence(raw, changed)
        self.assertEqual(packet['items'][0]['text'], changed)
        self.assertNotIn('10만원', str(packet))

    def test_long_and_control_text_is_skipped_in_full_not_tail_truncated(self):
        raw = facts(unverified_facts={'support_detail': '보류 값'})
        for source in ('가' * (MAX_REVIEW_SOURCE_CHARS + 1) + '\n신청 불가',
                       '교육비\x00신청불가', '지원\u202e제외'):
            with self.subTest(length=len(source)):
                packet = build_review_evidence(raw, source)
                self.assertEqual(packet['items'], [])
                self.assertEqual(packet['omitted_count'], 1)
        source = '가' * MAX_REVIEW_SOURCE_CHARS
        self.assertEqual(build_review_evidence(raw, source)['items'][0]['text'], source)

    def test_diagnostic_only_change_does_not_change_facts_or_decisions(self):
        source = '개인이 신청합니다.\n교육비 지원.\n온라인 신청 필수'
        raw = facts(support_detail='교육비 지원.', applicant_evidence='온라인 신청 필수')
        with patch('services.news_review_evidence.auxiliary_condition_fields', return_value=[]):
            baseline = assess(self.profile, copy.deepcopy(raw), source)
        updated = assess(self.profile, copy.deepcopy(raw), source)
        for field in ('accepted', 'review_required', 'target_status', 'target_checks', 'filter_reason'):
            self.assertEqual(updated[field], baseline[field])
        for key in baseline['analysis']:
            if key not in ('validation_diagnostics', 'grounding_note'):
                self.assertEqual(updated['analysis'][key], baseline['analysis'][key])
        before = copy.deepcopy(updated)
        build_review_evidence(updated['analysis'], source)
        self.assertEqual(updated, before)

    def test_frozen_real_responses_keep_false_support_and_overall_holds(self):
        _, responses, sources, _, hashes = load_frozen_cases()
        before = copy.deepcopy(responses)
        for case in ('news-seoul-wall', 'news-busan-jobfair'):
            with self.subTest(case=case):
                result = assess(self.profile, responses[case], sources[case])
                analysis = result['analysis']
                before_packet = copy.deepcopy(result)
                packet = build_review_evidence(analysis, sources[case])
                self.assertEqual(packet['items'][0]['text'], sources[case])
                self.assertEqual(result, before_packet)
                self.assertEqual(result['target_status'], 'review')
                self.assertFalse(result['accepted'])
                if case == 'news-busan-jobfair':
                    self.assertEqual(analysis['support_detail'], '')
                    self.assertNotIn('청년권 우수기업', str(packet))
                else:
                    self.assertTrue(any(item['origin'] == 'auxiliary_evidence_only'
                                        for item in analysis['validation_diagnostics']['condition_holds']),
                                    analysis['validation_diagnostics'])
        self.assertEqual(responses, before)
        for path, digest in hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_older_two_real_response_packets_do_not_repair_empty_support(self):
        root = Path(__file__).resolve().parents[2]
        ledger = root / 'artifacts/ai-live-validation-20260910/generation-live-01/ledger.jsonl'
        ledger_raw = ledger.read_bytes()
        self.assertEqual(hashlib.sha256(ledger_raw).hexdigest(),
                         '7ce0d34fd7bf827e409a7bfe9fffb936e85dc0f6d5268e1cd303fab7a125a6f2')
        rows = [json.loads(line) for line in ledger_raw.decode('utf-8').splitlines()]
        responses = {row['case_id']: json.loads(row['raw']['text']) for row in rows
                     if row['event'] == 'response' and row['case_id'] in ('news-market', 'news-provider')}
        self.assertEqual(set(responses), {'news-market', 'news-provider'})
        for key, raw in responses.items():
            with self.subTest(case=key):
                source_path = root / 'artifacts/public-document-eval-20260909/sources' / (key + '.txt')
                source_bytes = source_path.read_bytes()
                source = source_path.read_text(encoding='utf-8')
                with patch('services.news_review_evidence.auxiliary_condition_fields', return_value=[]):
                    baseline = assess(self.profile, copy.deepcopy(raw), source)
                result = assess(self.profile, copy.deepcopy(raw), source)
                for field in ('accepted', 'review_required', 'target_status', 'target_checks', 'filter_reason'):
                    self.assertEqual(result[field], baseline[field])
                self.assertEqual(result['analysis']['support_detail'], '')
                before = copy.deepcopy(result)
                packet = build_review_evidence(result['analysis'], source)
                self.assertEqual(packet['items'][0]['text'], source)
                self.assertEqual(result, before)
                self.assertEqual(result['target_status'], 'review')
                self.assertFalse(result['accepted'])
                self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(ledger.read_bytes(), ledger_raw)

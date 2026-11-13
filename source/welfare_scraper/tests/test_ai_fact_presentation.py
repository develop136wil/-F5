"""Offline facts/formatting regression; no model calls or operational writes."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from services import policy_evidence as policy, news_analysis_safety as news
from services import news_custom_analysis as custom
from services.news_profile import NewsProfile

FIXTURES = Path(__file__).resolve().parents[2] / 'artifacts/public-document-eval-20260909'


class PresentationTests(unittest.TestCase):
    def test_adjacent_label_and_value_not_a_new_fact(self):
        for label in ('모집대상', '신청자격', '지원금액'):
            with self.subTest(label=label):
                self.assertTrue(policy.preserves_quoted_facts(label + ' 누구나', label + '\n누구나'))

    def test_labels_keep_their_values_and_blank_boundaries(self):
        self.assertFalse(policy.preserves_quoted_facts(
            '지원금액: 2만원\n신청자격: 청년', '지원금액: 청년\n신청자격: 2만원'))
        self.assertFalse(policy.preserves_quoted_facts('모집대상 누구나', '모집대상\n\n누구나'))

    def test_negative_subject_reflow_stays_rejected(self):
        for negative in ('신청할 수 없음', '지원받을 수 없음', '지급하지 않음',
                         '대상이 될 수 없음', '해당되지 않음', '안 됨', '필요 없음', '지원하지 않음'):
            self.assertFalse(policy.preserves_quoted_facts(
                '지원대상 청년\n아동 대상\n' + negative,
                '지원대상\n청년\n' + negative + '\n아동 대상'))
        for quote, value in [
            ('아동 대상 아님\n청년 대상', '아동 대상\n아님 청년 대상'),
            ('모집대상\n아동 아님\n청년', '모집대상 아동\n아님 청년'),
            ('방문 신청\n불가 온라인 신청', '방문 신청 불가\n온라인 신청'),
            ('모집대상\n아동\n지원 불가', '모집대상 아동 지원 불가'),
            ('지원대상\n청년\n아님\n아동 대상', '지원대상 청년\n아동 대상\n아님'),
        ]:
            with self.subTest(quote=quote):
                self.assertFalse(policy.preserves_quoted_facts(value, quote))

    def test_url_link_stays_bound_to_application_channel(self):
        quote = '신청방법: 방문 또는 웹사이트\n(https://example.invalid/apply)\n모바일 앱 신청'
        value = '신청방법: 방문 또는 웹사이트(https://example.invalid/apply) 모바일 앱 신청'
        self.assertTrue(policy.preserves_quoted_facts(value, quote))
        self.assertFalse(policy.preserves_quoted_facts(value.replace('/apply', '/other'), quote))
        self.assertFalse(policy.preserves_quoted_facts(
            '신청 웹사이트(https://b.invalid)\n제외 웹사이트(https://a.invalid)',
            '신청 웹사이트(https://a.invalid)\n제외 웹사이트(https://b.invalid)'))

    def test_model_cannot_reflow_its_own_quote_to_bypass_source_grounding(self):
        source = '교육비 100만원\n아님 교통비 10만원'
        rewritten = '교육비 100만원 아님\n교통비 10만원'
        result = {'support_detail': rewritten, '_source_evidence': {'support_detail': rewritten}}
        self.assertFalse(policy.quote_matches_source(rewritten, source))
        self.assertFalse(policy.validate_policy_evidence(result, source))
        facts = dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
            target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
            life_cycle_evidence=[], residence_evidence=[], support_detail=rewritten,
            eligibility_detail='', contact='', deadline='',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.',
            applicant_detail='개인', beneficiary_detail='개인', evidence_snippet='',
            fact_evidence={'support_detail': rewritten})
        assessed = news.assess(NewsProfile('quote-regression', 1, '시험', '전국'),
                              facts, source + '\n개인이 신청합니다.')
        self.assertEqual(assessed['target_status'], 'review')
        self.assertEqual(assessed['analysis']['support_detail'], '')
        self.assertEqual(facts['support_detail'], rewritten)

    def test_detached_negative_cannot_be_omitted_from_short_true_quote(self):
        for negative in ('지급하지 않음', '미지급', '지급 중단', '미지원', '지원 중단'):
            with self.subTest(negative=negative):
                source = '교육비 100만원\n' + negative
                result = {'support_detail': '교육비 100만원',
                          '_source_evidence': {'support_detail': '교육비 100만원'}}
                self.assertFalse(policy.validate_policy_evidence(result, source))
                self.assertEqual(news._omitted_conditions(result, source), [negative])
                self.assertFalse(policy.preserves_quoted_facts(
                    '교육비 100만원\n교통비 10만원\n' + negative,
                    source + '\n교통비 10만원'))

    def test_real_snapshots_unchanged_and_only_presentation_hold_removed(self):
        raw_ledger = (FIXTURES / 'live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(raw_ledger).hexdigest(),
                         '34f3719364f287b539934ed862b0f33e0fd1c9b3eb74175869e4fc46c003483d')
        self.assertEqual(hashlib.sha256((FIXTURES / 'gold.json').read_bytes()).hexdigest(),
                         'e7794d83bf0c0025e75a5de43711199bb1e43debba52755af8856385b228005d')
        events = [json.loads(line) for line in raw_ledger.decode().splitlines()]
        plan = next(item for item in events if item['event'] == 'plan')
        for case in plan['cases']:
            content = (FIXTURES / 'sources' / (case['id'] + '.txt')).read_text(encoding='utf-8')
            self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), case['source_sha256'])
        results = {item['case_id']: json.loads(item['raw']['text']) for item in events
                   if item['event'] == 'response'}
        before = copy.deepcopy(results)
        source = lambda key: (FIXTURES / 'sources' / (key + '.txt')).read_text(encoding='utf-8')
        self.assertTrue(policy.validate_policy_evidence(results['policy-child'], source('policy-child')))
        self.assertFalse(policy.validate_policy_evidence(results['policy-first'], source('policy-first')))
        rent = copy.deepcopy(results['policy-rent'])
        rent['residence'], rent['target_characteristics'] = [], ['일반']
        self.assertFalse(policy.validate_policy_evidence(rent, source('policy-rent')))
        market = results['news-market']
        self.assertTrue(policy.preserves_quoted_facts(market['eligibility_detail'],
                                                    market['fact_evidence']['eligibility_detail']))
        self.assertFalse(policy.preserves_quoted_facts(market['support_detail'],
                                                     market['fact_evidence']['support_detail']))
        self.assertTrue(news._date_supported(results['news-provider']['deadline'], source('news-provider')))
        self.assertFalse(news._date_supported(market['deadline'], source('news-market')))
        self.assertEqual(results, before)

    def test_duration_never_establishes_recipient_age(self):
        result = {'start_age_months': 0, 'end_age_months': 24}
        issues = policy.structured_policy_issues(result, '지원대상: 출생아\n사용기간: 출생일부터 2년간 사용')
        self.assertIn('start_age_months', issues)
        self.assertIn('end_age_months', issues)


class PromptContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_prompt_rules_do_not_change_old_queued_recipe(self):
        self.assertFalse(custom.recipe_options(custom.ALL_FACTS_RECIPE)['explicit_facts_rules'])
        self.assertTrue(custom.recipe_options(custom.RECIPE)['explicit_facts_rules'])
        profile = NewsProfile('format-regression', 1, '시험', '전국', general_mode='custom_v1')
        payload = dict(title='시험', link='https://example.invalid', site_name='시험', content='공개 자료')
        for recipe, expected in ((custom.RECIPE, True), (custom.ALL_FACTS_RECIPE, False)):
            with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value={})) as call:
                await custom.classify(object(), profile, payload, 'mock',
                                     explicit_facts_rules=custom.recipe_options(recipe)['explicit_facts_rules'])
                self.assertEqual('Voucher validity' in call.call_args.args[1], expected)
                self.assertEqual(call.await_count, 1)


if __name__ == '__main__':
    unittest.main()

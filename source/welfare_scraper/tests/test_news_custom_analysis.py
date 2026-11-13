import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services.news_profile import NewsProfile
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_custom_analysis as custom
from services.news_analysis_worker import process_job, RECIPE


def tearDownModule():
    import database
    database.close_all_connections()


class TestCustomAnalysis(unittest.IsolatedAsyncioTestCase):
    def test_v10_scoped_context_does_not_change_v9_contract(self):
        from services.news_fact_grounding import grounded_facts
        facts = {'life_cycle':[], 'residence':['서울특별시 도봉구'], 'life_cycle_evidence':[],
                 'residence_evidence':[{'value':'서울특별시 도봉구','quote':'도봉구 거주자','basis':'explicit'}]}
        for recipe, expected in ((custom.CONTEXT_RECIPE, []), (custom.SOURCE_SPAN_RECIPE, ['서울특별시 도봉구'])):
            options = custom.recipe_options(recipe)
            result, _ = grounded_facts(facts, '도봉구 거주자 대상, 중복 신청 불가.',
                **{key:options[key] for key in ('short_regions','strict_context','scoped_context')})
            self.assertEqual(result['residence'],expected)
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.profile = NewsProfile('custom', 1, '노년', '전국', min_age_months=780,
            life_cycles=('노년',), characteristics=('독거',), interest_topics=('주거',),
            site_ids=('1',), ai_prompt='고정 요청')
        profiles.create_profile(self.conn, self.profile)
        self.run = profiles.prepare_run(self.conn, 'custom', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        self.source = '전국 거주 노년 대상. 만 65세 이상 독거 가구에게 주거 지원. 개인이 신청합니다.'
        self.payload = dict(title='지원', link='https://example.invalid', site_name='시험', content=self.source + '\n' + 'x' * 7000)

    def tearDown(self):
        self.conn.close()

    async def call_db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    def enqueue(self, recipe=custom.SOURCE_SPAN_RECIPE):
        return jobs.enqueue(self.conn, self.run, payload=self.payload, model='fixed-model', recipe=recipe)

    async def test_v9_negative_context_is_review_and_v8_cache_is_separate(self):
        self.payload['content'] = '전국 거주 신청 가능합니다.\n제외 대상: 노년 대상\nx'
        keys = []
        for recipe, expected in ((custom.ALIAS_RECIPE, 'review'), (custom.SOURCE_SPAN_RECIPE, 'review')):
            job = self.enqueue(recipe)
            keys.append(jobs.get_job(self.conn, job)['cache_key'])
            with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=self.facts()):
                self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
            output = json.loads(jobs.get_job(self.conn, job)['result_json'])
            self.assertEqual(output['target_status'], expected)
            if recipe == custom.SOURCE_SPAN_RECIPE:
                self.assertFalse(output['accepted'])
                self.assertTrue(output['review_required'])
                self.assertIn('생애주기', output['analysis']['grounding_note'])
        self.assertNotEqual(*keys)

    def facts(self):
        return dict(is_relevant=True, category='복지', target='노년', residence=['전국'],
            life_cycle=['노년'], target_characteristics=['독거'], interest_topics=['주거'],
            start_age_months=780, end_age_months=None, evidence_snippet='',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.',
            applicant_detail='노년 신청자', beneficiary_detail='노년',
            life_cycle_evidence=[dict(value='노년', quote='노년 대상', basis='explicit')],
            residence_evidence=[dict(value='전국', quote='전국 거주', basis='explicit')])

    def test_missing_fields_and_non_string_tags_rejected(self):
        for key in ('residence', 'life_cycle', 'target_characteristics', 'interest_topics',
                    'start_age_months', 'end_age_months', 'evidence_snippet'):
            value = self.facts()
            del value[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                custom.validate_facts(value)
        with self.assertRaises(ValueError):
            custom.validate_facts(self.facts() | {'target_characteristics': [False]})

    async def test_non_news_is_excluded_even_if_target_matches(self):
        self.payload['content'] += '\n직원 채용 공고입니다.'
        job = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                   return_value=self.facts() | {'is_relevant': False, 'evidence_snippet': '직원 채용 공고입니다.'}):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['target_status'], 'excluded')
        self.assertEqual(output['filter_reason'], 'not_relevant')
        self.assertFalse(output['accepted'])

    async def test_match_and_prompt_use_fixed_context(self):
        job = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=self.facts()) as ai:
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['target_status'], 'match')
        self.assertTrue(output['accepted'])
        self.assertFalse(output['review_required'])
        prompt = ai.call_args.args[1]
        self.assertIn('독거', prompt)
        self.assertIn('interest_topics', prompt)
        self.assertIn('주거', prompt)
        self.assertIn('고정 요청', prompt)
        source = json.loads(prompt.split('\nSOURCE: ')[1])
        self.assertLessEqual(len(source['content']), 6000)
        self.assertGreater(len(source['content']), 5500)
        self.assertEqual(output['analysis']['source_input']['status'], 'partial')
        visible = jobs.list_profile_results(self.conn, 'custom')['items'][0]
        self.assertIn('빠진 부분', visible['input_note'])
        self.assertNotIn('content', visible)
        self.assertEqual(ai.call_args.kwargs['model_override'], 'fixed-model')
        schema = ai.call_args.kwargs['response_json_schema']
        self.assertEqual(schema['type'], 'object')
        self.assertIn('applicant_type', schema['required'])
        from google.genai.types import GenerateContentConfig
        self.assertEqual(GenerateContentConfig(response_json_schema=schema).response_json_schema, schema)

    async def test_missing_evidence_stays_review_in_saved_result_and_cache(self):
        job = self.enqueue()
        facts = self.facts() | {'start_age_months': None, 'life_cycle': [], 'target_characteristics': []}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['target_status'], 'review')
        self.assertTrue(output['review_required'])
        self.assertFalse(output['accepted'])
        cached = json.loads(self.conn.execute('SELECT result_json FROM news_analysis_cache').fetchone()[0])
        self.assertEqual(cached, output)

    async def test_definite_age_exclusion(self):
        self.payload['content'] = self.source.replace('만 65세 이상', '생후 12~60개월').replace('노년 대상', '영유아 대상')
        job = self.enqueue()
        facts = self.facts() | {'start_age_months': 12, 'end_age_months': 60,
                               'life_cycle': [], 'life_cycle_evidence': []}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        self.assertEqual(json.loads(jobs.get_job(self.conn, job)['result_json'])['target_status'], 'excluded')

    async def test_definite_interest_topic_exclusion(self):
        self.payload['content'] = self.source.replace('주거 지원', '교육 지원')
        job = self.enqueue()
        facts = self.facts() | {'interest_topics': ['교육']}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['target_status'], 'excluded')
        self.assertEqual(output['target_checks']['interest_topics'], 'excluded')

    async def test_bad_tags_fail_without_cache(self):
        job = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                   return_value=self.facts() | {'life_cycle': '노년'}):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_legacy_recipe_cannot_execute_custom_profile(self):
        job = self.enqueue(RECIPE)
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock) as ai:
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'failed')
            ai.assert_not_called()

    async def test_provider_error_waits_without_cache(self):
        job = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, side_effect=RuntimeError('429 rate limit')):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db, clock=lambda: 100), 'waiting')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'rate_limited')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_old_queued_recipe_keeps_prefix_and_new_cache_is_separate(self):
        old_job = self.enqueue(custom.OLD_RECIPE)
        new_job = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, old_job)['cache_key'],
                            jobs.get_job(self.conn, new_job)['cache_key'])
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=self.facts()) as ai:
            self.assertEqual(await process_job(old_job, object(), call_db=self.call_db), 'complete')
        source = json.loads(ai.call_args.args[1].split('\nSOURCE: ')[1])
        self.assertEqual(source['content'], self.payload['content'][:6000])
        self.assertNotIn('[Input coverage]', ai.call_args.args[1])
        self.assertNotIn('source_input', json.loads(jobs.get_job(self.conn, old_job)['result_json'])['analysis'])

    async def test_v12_queue_keeps_prompt_and_v13_uses_a_separate_cache(self):
        old = self.enqueue(custom.EXPLICIT_FACTS_RECIPE)
        new = self.enqueue(custom.SOURCE_SPAN_RECIPE)
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'],
                            jobs.get_job(self.conn, new)['cache_key'])
        for job, source_span_rules in ((old, False), (new, True)):
            with self.subTest(source_span_rules=source_span_rules):
                with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                           return_value=self.facts()) as ai:
                    self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
                self.assertEqual('[Source spans first; presentation second]' in ai.call_args.args[1],
                                 source_span_rules)
                self.assertIn('Voucher validity', ai.call_args.args[1])
                self.assertEqual(ai.call_args.kwargs['request_attempts'], 1)

    async def test_prior_recipe_without_interest_topics_remains_processable(self):
        job = self.enqueue(custom.PRIOR_RECIPE)
        old_facts = self.facts()
        old_facts.pop('interest_topics')
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=old_facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['target_status'], 'review')
        self.assertEqual(output['target_checks']['interest_topics'], 'review')

    async def test_applicant_gate_distinguishes_proxy_organization_and_unknown(self):
        for index, (role, evidence, expected) in enumerate([
            ('organization', '식품 판매 업소 영업자 모집', 'excluded'),
            ('proxy', '보호자가 자녀를 대신하여 신청', 'match'),
            ('individual', '개인이 신청', 'match'),
            ('unknown', '', 'review'),
            ('mixed', '개인과 기관 각각 신청', 'review'),
            ('organization', '원문에 없는 문장', 'review'),
        ]):
            with self.subTest(role=role, evidence=evidence):
                self.payload['content'] = self.source + ' 식품 판매 업소 영업자 모집. 보호자가 자녀를 대신하여 신청. 개인이 신청. 개인과 기관 각각 신청.'
                self.payload['link'] = f'https://example.invalid/{index}'
                job = self.enqueue()
                facts = self.facts() | dict(applicant_type=role, applicant_evidence=evidence)
                with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts) as ai:
                    self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
                output = json.loads(jobs.get_job(self.conn, job)['result_json'])
                self.assertEqual(output['target_status'], expected)
                self.assertEqual(output['accepted'], expected == 'match')
                self.assertIn('A parent applying', ai.call_args.args[1])
                visible = next(r for r in jobs.list_profile_results(self.conn, 'custom')['items'] if r['job_id'] == job)
                self.assertEqual(visible['details'].get('applicant_evidence', ''), evidence)

    async def test_v4_keeps_original_contract_and_separate_cache(self):
        old = self.enqueue(custom.TOPICS_RECIPE)
        new = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'], jobs.get_job(self.conn, new)['cache_key'])
        facts = {k:v for k,v in self.facts().items() if not k.startswith('applicant_') and k != 'beneficiary_detail'}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts) as ai:
            self.assertEqual(await process_job(old, object(), call_db=self.call_db), 'complete')
        self.assertNotIn('Separate who applies', ai.call_args.args[1])
        self.assertEqual(json.loads(jobs.get_job(self.conn, old)['result_json'])['target_status'], 'review')

    async def test_cache_preserves_applicant_gate_without_another_ai_call(self):
        for index, (role, expected) in enumerate((('organization', 'excluded'),
                                                ('proxy', 'match'),
                                                ('unknown', 'review'))):
            with self.subTest(role=role):
                payload = self.payload | {'link': f'https://example.invalid/cache-{index}'}
                quote = {'organization': '식품 판매 업소 영업자 모집',
                         'proxy': '보호자가 자녀를 대신하여 신청', 'unknown': ''}[role]
                payload['content'] += '\n' + quote
                first = jobs.enqueue(self.conn, self.run, payload=payload,
                                     model='fixed-model', recipe=custom.SOURCE_SPAN_RECIPE)
                facts = self.facts() | {'applicant_type': role, 'applicant_evidence': quote}
                with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                           return_value=facts) as ai:
                    self.assertEqual(await process_job(first, object(), call_db=self.call_db), 'complete')
                    next_run = profiles.prepare_run(self.conn, 'custom', expected_revision=1,
                        sites=[dict(id=1, name='시험', url='https://example.invalid')])
                    cached = jobs.enqueue(self.conn, next_run, payload=payload,
                                          model='fixed-model', recipe=custom.SOURCE_SPAN_RECIPE)
                    self.assertEqual(await process_job(cached, object(), call_db=self.call_db), 'not_claimed')
                    self.assertEqual(ai.await_count, 1)
                original = json.loads(jobs.get_job(self.conn, first)['result_json'])
                reused = json.loads(jobs.get_job(self.conn, cached)['result_json'])
                self.assertEqual(reused['target_status'], expected)
                self.assertEqual(reused['target_checks'], original['target_checks'])
                self.assertEqual(reused['accepted'], original['accepted'])
                self.assertEqual(jobs.get_job(self.conn, cached)['attempts'], 0)

    async def test_invalid_applicant_contract_is_not_cached(self):
        job = self.enqueue()
        facts = self.facts() | {'applicant_type': 'invented'}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_v5_preserves_applicant_gate_without_new_schema(self):
        old = self.enqueue(custom.APPLICANT_RECIPE)
        new = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'], jobs.get_job(self.conn, new)['cache_key'])
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=self.facts()) as ai:
            self.assertEqual(await process_job(old, object(), call_db=self.call_db), 'complete')
        self.assertNotIn('response_json_schema', ai.call_args.kwargs)
        self.assertIn('Separate who applies', ai.call_args.args[1])

    async def test_non_object_never_becomes_success(self):
        job = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=[self.facts()]):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_ungrounded_tags_are_review_and_warning_is_visible(self):
        job = self.enqueue()
        facts = self.facts() | {'life_cycle_evidence': []}
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertFalse(output['accepted'])
        self.assertEqual(output['target_status'], 'review')
        self.assertEqual(output['analysis']['life_cycle'], [])
        self.assertIn('명시적', jobs.list_profile_results(self.conn, 'custom')['items'][0]['input_note'])

    async def test_v6_does_not_require_new_evidence(self):
        job = self.enqueue(custom.SCHEMA_RECIPE)
        facts = self.facts()
        del facts['life_cycle_evidence']
        del facts['residence_evidence']
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts) as ai:
            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
        self.assertNotIn('life_cycle_evidence', ai.call_args.kwargs['response_json_schema']['properties'])
        self.assertEqual(json.loads(jobs.get_job(self.conn, job)['result_json'])['target_status'], 'review')

    async def test_v7_keeps_strict_region_while_v8_accepts_reviewed_alias(self):
        quote = '도봉구에 거주하는 가족 5팀 (도봉구민 만 참여가능)'
        profile = NewsProfile('dobong', 1, '도봉 가족', '서울특별시 도봉구', site_ids=('1',))
        profiles.create_profile(self.conn, profile)
        run = profiles.prepare_run(self.conn, 'dobong', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        payload = self.payload | {'content': quote + ' 개인이 신청'}
        facts = self.facts() | {'residence': ['서울특별시 도봉구'], 'life_cycle': [],
            'start_age_months': None, 'target_characteristics': [], 'interest_topics': [],
            'life_cycle_evidence': [], 'residence_evidence': [dict(value='서울특별시 도봉구', quote=quote, basis='explicit')],
            'applicant_evidence': '개인이 신청'}
        keys = []
        for recipe, expected in ((custom.GROUNDING_RECIPE, 'match'), (custom.ALIAS_RECIPE, 'match'), (custom.SOURCE_SPAN_RECIPE, 'match')):
            job = jobs.enqueue(self.conn, run, payload=payload, model='fixed-model', recipe=recipe)
            keys.append(jobs.get_job(self.conn, job)['cache_key'])
            with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts):
                self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
            self.assertEqual(json.loads(jobs.get_job(self.conn, job)['result_json'])['target_status'], expected)
        self.assertEqual(len(set(keys)), 3)

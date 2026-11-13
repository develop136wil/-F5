"""Synthetic Korean requests and isolated quota ledgers; no live provider use."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as coordinator, ai_rate_policy as rate
from services import policy_source_plan as policy
from services.news_request_plan import build_news_request


SHORT_SOURCE = '지원내용\n교통비 월 3만원\n신청방법\n온라인 신청\n제출서류\n신분증'
MODERATE_SOURCE = SHORT_SOURCE + '''
방문 신청은 거주지 주민센터에서 접수하며 온라인 신청은 해당 기관 누리집에서 진행합니다.
대리 신청을 하는 경우에는 위임장과 대리인의 신분증을 함께 제출해야 합니다.
서류를 제출한 다음에는 접수번호를 안내받고 신청 내용을 다시 확인해야 합니다.
증빙서류가 빠진 경우에는 담당 기관의 안내에 따라 정해진 기간 안에 보완해야 합니다.
신청 내용이 변경되면 담당 기관에 변경사항을 알려야 하며 처리 결과는 별도로 안내합니다.
공휴일에는 방문 접수를 받지 않으므로 평일 운영시간을 확인한 뒤 방문해야 합니다.
접수한 서류는 심사를 위해 사용되며 사실과 다르게 작성한 경우에는 지원이 취소됩니다.
지원금은 신청인 명의의 계좌로 지급하며 계좌정보를 정확하게 작성해야 합니다.
심사 과정에서 추가 확인이 필요한 경우 담당 기관에서 필요한 자료를 요청할 수 있습니다.
동일한 목적으로 이미 지원을 받은 경우에는 중복 지급 여부를 확인한 다음 결과를 안내합니다.'''
OPTIONS = {'category': ['의료/재활', '교육/보육', '가족지원', '돌봄/양육', '복지', '생활지원'],
           'target_characteristics': ['일반', '한부모'], 'residence': ['서울시'],
           'life_cycle': ['영아', '유아']}


def policy_request(source=SHORT_SOURCE, **kwargs):
    return policy.build_policy_extraction_request(source, '교통비 지원',
        'https://example.invalid/transport', '복지센터', OPTIONS, **kwargs)


class TestCompleteGenerationPlanning(unittest.TestCase):
    def test_estimate_includes_schema_and_utf8_without_changing_allowance(self):
        prompt, schema = policy_request()
        self.assertEqual(coordinator.estimate_generation_input_tokens(prompt, schema),
            coordinator.estimate_input_tokens([prompt, json.dumps(schema,
                ensure_ascii=False, separators=(',', ':'))]))
        self.assertEqual(coordinator.estimate_generation_input_tokens('정책'),
                         coordinator.estimate_input_tokens('정책'))
        self.assertGreater(coordinator.estimate_generation_input_tokens(prompt, schema), 6000)

    def test_short_and_moderate_policy_fit_with_identical_schema_and_all_source(self):
        before = copy.deepcopy(OPTIONS)
        for source in (SHORT_SOURCE, MODERATE_SOURCE.replace('\n', '\r\n')):
            with self.subTest(chars=len(source)):
                full, full_schema = policy_request(source)
                prompt, schema = policy_request(source, input_token_budget=6000)
                self.assertLessEqual(coordinator.estimate_generation_input_tokens(prompt, schema), 6000)
                self.assertNotEqual(full, prompt)
                self.assertEqual(schema, full_schema)
                units = json.loads(prompt.split('Units (complete original text): ')[1].split('\nAllowed options: ')[0])
                self.assertEqual(''.join(unit['text'] for unit in units), source)
                self.assertEqual(prompt.count('교통비 월 3만원'), 1)
        self.assertEqual(OPTIONS, before)

    def test_roomy_request_keeps_full_prompt_and_exact_compact_boundary_is_respected(self):
        original = policy_request()
        self.assertEqual(policy_request(input_token_budget=50000), original)
        compact = policy_request(compact=True)
        size = coordinator.estimate_generation_input_tokens(*compact)
        self.assertEqual(policy_request(input_token_budget=size), compact)
        with self.assertRaises(policy.PolicySourcePlanError) as caught:
            policy_request(input_token_budget=size - 1)
        self.assertEqual(caught.exception.code, 'source_input_budget_exceeded')

    def test_source_that_cannot_fit_is_not_cut_or_split(self):
        source = MODERATE_SOURCE * 5
        self.assertLess(len(source), 6000)
        with self.assertRaises(policy.PolicySourcePlanError) as caught:
            policy_request(source, input_token_budget=6000)
        self.assertEqual(caught.exception.code, 'source_input_budget_exceeded')
        for budget in (0, -1, True, '6000'):
            with self.subTest(budget=budget), self.assertRaises(policy.PolicySourcePlanError):
                policy_request(input_token_budget=budget)


class TestNewsRequestPlanning(unittest.TestCase):
    def build(self, source, **kwargs):
        return build_news_request('교통비 지원', 'https://example.invalid/news', '복지센터',
            source, kwargs.pop('custom_prompt', ''), input_token_budget=kwargs.pop('input_token_budget', 6000),
            **kwargs)

    def test_complete_korean_body_fits_and_retains_output_contract(self):
        prompt, coverage = self.build(SHORT_SOURCE)
        self.assertLessEqual(coordinator.estimate_generation_input_tokens(prompt), 6000)
        self.assertEqual(json.loads(prompt.split('\nSOURCE: ')[1]), SHORT_SOURCE)
        self.assertEqual(coverage['status'], 'complete')
        shape = json.loads(prompt.split('\nShape: ')[1].split('\nMetadata: ')[0])
        self.assertEqual(set(shape), {'is_relevant', 'category', 'target', 'target_characteristics',
            'start_age_months', 'end_age_months', 'age_detail', 'target_income', 'eligibility_detail',
            'residence', 'life_cycle', 'support_detail', 'contact', 'deadline', 'event_date',
            'event_end_date', 'page_modified_date', 'related_urls', 'source_site_name',
            'source_site_url', 'item_type', 'reasoning', 'evidence_snippet'})

    def test_long_korean_news_uses_disclosed_excerpts_under_full_request_allowance(self):
        source = MODERATE_SOURCE * 12 + '\n신청 기간: 2026-09-30\n신청자는 원문 전체를 확인하세요.'
        before = source
        prompt, coverage = self.build(source, content_max_chars=6000)
        self.assertLessEqual(coordinator.estimate_generation_input_tokens(prompt), 6000)
        self.assertEqual(coverage['status'], 'partial')
        self.assertTrue(coverage['budget_limited'])
        self.assertEqual(coverage['total_chars'], len(source))
        self.assertEqual(coverage['requested_max_chars'], 6000)
        self.assertIn('[Input coverage]', prompt)
        self.assertIn('2026-09-30', prompt)
        self.assertEqual(source, before)

    def test_metadata_custom_instructions_and_contacts_are_never_cut_to_fit(self):
        custom = '유아를 위한 지원 공고에 우선순위를 부여하세요.'
        prompt, coverage = self.build(MODERATE_SOURCE * 10, custom_prompt=custom,
            contact_metadata={'status': 'minimized'}, source_page={'status': 'review_required',
                                                                  'reasons': ['page_char_limit']})
        self.assertIn(custom, prompt)
        self.assertIn('[Local contact minimization]', prompt)
        self.assertLessEqual(coordinator.estimate_generation_input_tokens(prompt), 6000)
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            self.build(SHORT_SOURCE, custom_prompt=custom * 200)

    def test_sensitive_tail_is_checked_before_excerpt_selection(self):
        from services.ai_input_safety import AiInputReviewRequired
        with self.assertRaises(AiInputReviewRequired):
            self.build(MODERATE_SOURCE * 20 + '\n010-1234-5678')


class TestUnchangedGenerationAdmission(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        temporary = tempfile.TemporaryDirectory(prefix='generation-budget-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch.object(coordinator, '_state_path', return_value=str(self.root / 'state.json'))
        patched.start()
        self.addCleanup(patched.stop)
        env = patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '20'})
        env.start()
        self.addCleanup(env.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)

    async def assert_policy_sdk_call(self, source):
        import ai_classifier
        from google.genai import types
        from services.policy_source_units import source_units
        raw = {'_source_units': {'support_detail': [u['id'] for u in source_units(source)],
                'eligibility_detail': [], 'age_detail': [], 'target_income': []},
               'category': '복지', 'target_characteristics': [], 'residence': [],
               'life_cycle': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw))]), finish_reason='STOP')])
        generate = AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': rate.MODELS[0], 'AI_CONTENT_MAX_CHARS': '6000'}):
            result = await ai_classifier.extract_policy_fields_async(client, '교통비 지원',
                'https://example.invalid/transport', '복지센터', content=source,
                taxonomy=OPTIONS, review=review)
        generate.assert_awaited_once()
        self.assertIsNotNone(result, review)
        self.assertEqual(result['support_detail'], source)
        actual = generate.await_args.kwargs
        self.assertLessEqual(coordinator.estimate_generation_input_tokens(
            actual['contents'], actual['config'].response_json_schema), 6000)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)

    async def test_short_policy_succeeds_through_guarded_sdk_call(self):
        await self.assert_policy_sdk_call(SHORT_SOURCE)

    async def test_moderate_policy_succeeds_through_guarded_sdk_call(self):
        await self.assert_policy_sdk_call(MODERATE_SOURCE)

    async def test_oversize_policy_is_held_without_sdk_call_or_daily_reservation(self):
        import ai_classifier
        generate = AsyncMock(side_effect=AssertionError('Oversize source must not reach provider'))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate,
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=10000)))))
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': rate.MODELS[0], 'AI_CONTENT_MAX_CHARS': '6000'}):
            result = await ai_classifier.extract_policy_fields_async(client, '교통비 지원',
                'https://example.invalid/transport', '복지센터', content=MODERATE_SOURCE * 5,
                taxonomy=OPTIONS, review=review)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_budget_exceeded')
        generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_long_news_uses_one_guarded_sdk_request_and_reports_real_coverage(self):
        import ai_classifier
        from google.genai import types
        source = MODERATE_SOURCE * 12 + '\n신청 기간: 2026-09-30'
        raw = {'is_relevant': True, 'category': '복지', 'target': '일반',
               'source_input': {'status': 'complete'}}
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw))]), finish_reason='STOP')])
        generate = AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        result = await ai_classifier.classify_post_async(client, '교통비 지원',
            'https://example.invalid/news', '복지센터', content=source, custom_prompt='',
            model_override=rate.MODELS[0], content_max_chars=6000, strict=True, request_attempts=1)
        generate.assert_awaited_once()
        self.assertTrue(result['is_relevant'])
        self.assertEqual(result['source_input']['status'], 'partial')
        self.assertTrue(result['source_input']['budget_limited'])
        self.assertEqual(result['source_input']['total_chars'], len(source))
        self.assertLessEqual(coordinator.estimate_generation_input_tokens(
            generate.await_args.kwargs['contents']), 6000)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)

    async def test_news_instructions_too_large_are_held_without_sdk_or_reservation(self):
        import ai_classifier
        generate = AsyncMock(side_effect=AssertionError('Oversize instructions must not reach provider'))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            await ai_classifier.classify_post_async(client, '교통비 지원',
                'https://example.invalid/news', '복지센터', content=SHORT_SOURCE,
                custom_prompt='유아를 위한 지원 공고에 우선순위를 부여하세요.' * 200,
                model_override=rate.MODELS[0], strict=True, request_attempts=1)
        generate.assert_not_awaited()
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

    async def test_fitted_policy_is_reserved_under_unchanged_default_limit(self):
        model = rate.MODELS[0]
        budget = coordinator.input_token_budget(model)
        self.assertEqual(budget, 6000)
        prompt, schema = policy_request(MODERATE_SOURCE, input_token_budget=budget)
        size = coordinator.estimate_generation_input_tokens(prompt, schema)
        await coordinator.acquire_model_slot(model, input_tokens=size)
        ledger = json.loads((self.root / 'state.json.minute.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['entries'][0][2], size)
        self.assertEqual(coordinator.input_token_budget(model), 6000)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 1)

    async def test_smaller_current_budget_and_429_penalty_remain_authoritative(self):
        model, embed = rate.MODELS
        key = 'synthetic-generation-budget-key'
        observation = rate.observe({model: {'rpm': 10, 'tpm': 5000, 'rpd': 20},
            embed: {'rpm': 10, 'tpm': 10000, 'rpd': 20}}, key=key)
        (self.root / 'config.json').write_text(json.dumps({'GOOGLE_API_KEY': key,
            rate.POLICY_KEY: observation}), encoding='utf-8')
        self.assertEqual(coordinator.input_token_budget(model), 3000)
        with self.assertRaises(policy.PolicySourcePlanError):
            policy_request(input_token_budget=coordinator.input_token_budget(model))
        with self.assertRaises(coordinator.AiInputTokenBudgetExceeded):
            await coordinator.acquire_model_slot(model, input_tokens=3001)
        window = rate.read_window(str(self.root / 'state.json.minute.json'))
        rate.lower_after_rate_error(window, model, 2)
        (self.root / 'state.json.minute.json').write_text(json.dumps(window), encoding='utf-8')
        self.assertEqual(coordinator.input_token_budget(model), 1500)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 0)

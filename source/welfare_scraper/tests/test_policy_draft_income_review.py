"""Offline new-policy preview regression: no real AI, Notion or user DB writes."""
import copy
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import ai_classifier
import api_server
from scraper_engine import ScraperEngine
from services import ai_coordinator, notion_service, policy_sync
from services.policy_evidence import diagnose_policy_evidence, validate_policy_evidence
from services.policy_source_units import source_units
from services.policy_source_plan import build_policy_extraction_request
from services.ai_rate_policy import estimate_input_tokens
from services.ai_input_safety import inspect_ai_text
from tests.test_policy_url_draft import SCHEMA, _Session


INCOME = ('청소년한부모 소득 기준\n'
          '복지급여: 기준 중위소득 65% 이하\n'
          '한부모가족증명서 발급(비급여): 기준 중위소득 72% 이하')
SOURCE = '지원 내용\n아동 양육비 지원\n소득 기준\n' + INCOME


def response(source=SOURCE):
    return {'support_detail': source, 'target_income': INCOME,
            '_source_evidence': {'support_detail': source, 'target_income': INCOME}}


class DraftIncomeExtraction(unittest.IsolatedAsyncioTestCase):
    async def extract(self, source=SOURCE, payload=None, **kwargs):
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json',
                          new=AsyncMock(return_value=payload if payload is not None else response(source))) as call:
            result = await ai_classifier.extract_policy_fields_async(
                object(), '한부모 지원', 'https://example.org/policy', content=source,
                review=review, **kwargs)
        return result, review, call

    async def test_preview_preserves_both_purposes_without_retry(self):
        original = response()
        result, review, call = await self.extract(payload=copy.deepcopy(original), draft_preview=True)
        self.assertEqual(result, original)
        self.assertEqual(review['code'], 'draft_income_review_required')
        self.assertEqual(review['fields'], ['target_income'])
        self.assertEqual(call.await_count, 1)
        self.assertFalse(validate_policy_evidence(result, SOURCE), 'Preview must not relax update approval')

    async def test_existing_policy_and_missing_review_sink_still_hold_before_ai(self):
        result, review, call = await self.extract()
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_criteria_review_required')
        call.assert_not_awaited()
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            result = await ai_classifier.extract_policy_fields_async(
                object(), '정책', 'https://example.org', content=SOURCE, draft_preview=True)
        self.assertIsNone(result)
        call.assert_not_awaited()

    async def test_unknown_scopes_are_reviewed_not_resolved(self):
        source = '복지급여 기준 중위소득 65% 이하\n복지급여 기준 중위소득 72% 이하'
        payload = {'support_detail': source, 'target_income': source,
                   '_source_evidence': {'support_detail': source, 'target_income': source}}
        result, review, call = await self.extract(source, payload, draft_preview=True)
        self.assertEqual(result['target_income'], source)
        self.assertEqual(review['fields'], ['target_income'])
        self.assertEqual(call.await_count, 1)

    async def test_model_cannot_choose_one_threshold_or_invent_numbers(self):
        for text in ('아동 양육비 지원\n복지급여: 기준 중위소득 65% 이하',
                     SOURCE.replace('65%', '80%')):
            payload = {'support_detail': text, '_source_evidence': {'support_detail': text}}
            with self.subTest(text=text):
                result, review, call = await self.extract(payload=payload, draft_preview=True)
                self.assertIsNone(result)
                self.assertNotEqual(review['code'], 'draft_income_review_required')
                self.assertEqual(call.await_count, 1)

    async def test_other_source_holds_still_block_before_ai(self):
        for source in ('', '가' * 6001, SOURCE + '\n신청 월 만 18세가 되는 청년'):
            with self.subTest(size=len(source)), patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}):
                result, review, call = await self.extract(source=source, draft_preview=True)
                self.assertIsNone(result)
                self.assertNotEqual(review['code'], 'draft_income_review_required')
                call.assert_not_awaited()

    async def test_new_unit_contract_also_preserves_every_income_condition(self):
        # Use the current production response format, not only legacy prose.
        ids = [unit['id'] for unit in source_units(SOURCE)]
        payload = {'_source_units': {'support_detail': ids, 'target_income': ids,
                                    'eligibility_detail': [], 'age_detail': []},
                   'category': '', 'target_characteristics': [], 'residence': [],
                   'life_cycle': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}
        result, review, call = await self.extract(payload=payload, draft_preview=True)
        self.assertIsNotNone(result, review)
        self.assertIn('65%', result['target_income'])
        self.assertIn('72%', result['target_income'])
        self.assertEqual(review['code'], 'draft_income_review_required')
        self.assertEqual(call.await_count, 1)

    def test_preview_diagnostics_do_not_skip_fact_or_age_validation(self):
        valid = diagnose_policy_evidence(response(), SOURCE, draft_income_review=True)
        self.assertTrue(valid['valid'])
        invalid = response()
        invalid['target_income'] = INCOME.replace('65%', '80%')
        self.assertFalse(diagnose_policy_evidence(invalid, SOURCE, draft_income_review=True)['valid'])
        age = SOURCE + '\n신청 월 만 18세가 되는 청년'
        self.assertFalse(diagnose_policy_evidence(response(age), age, draft_income_review=True)['valid'])

    async def test_sensitive_email_is_not_mislabelled_as_a_secret_key(self):
        review = {}
        with patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as reserve:
            result = await ai_classifier.extract_policy_fields_async(
                object(), '정책', 'https://example.org',
                content='지원내용\n교육비 지원\n문의: person@example.org', review=review, draft_preview=True)
        self.assertIsNone(result)
        self.assertIn('이메일 주소', review['note'])
        self.assertNotIn('비밀키', review['note'])
        self.assertNotIn('person@', review['note'])
        reserve.assert_not_awaited()

    async def test_input_budget_hold_has_actionable_reason_and_no_retry(self):
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                side_effect=ai_coordinator.AiInputTokenBudgetExceeded('test'))) as call:
            result = await ai_classifier.extract_policy_fields_async(
                object(), '정책', 'https://example.org', content='교육비 지원', review=review, draft_preview=True)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'draft_input_budget_exceeded')
        self.assertIn('같은 입력', review['note'])
        call.assert_awaited_once()


class DraftInputPreparation(unittest.TestCase):
    def test_official_body_and_title_leave_other_workflows_unchanged(self):
        parsed = ai_classifier._parse_page_html(
            '<title>기관명</title><div>기관 공통 안내</div><h2 id="contents_title">정책명</h2>'
            '<div id="contents_body"><h3>지원대상</h3>유료회원<h3>지원내용</h3>도서 대여</div>',
            'https://www.mohw.go.kr/menu.es?mid=example')
        self.assertEqual(parsed['draft_title'], '정책명')
        self.assertNotIn('기관 공통 안내', parsed['draft_text'])
        self.assertIn('유료회원', parsed['draft_text'])
        self.assertIn('도서 대여', parsed['draft_text'])
        self.assertEqual(parsed['title'], '기관명')
        self.assertIn('기관 공통 안내', parsed['policy_text'])

    def test_library_sections_do_not_become_one_age_or_income_unit(self):
        source = ('### 도서 대여\n그림책 열람\n### 이용대상\n유료회원\n'
                  '### 이용안내\n15권 대여\n### 이용시간\n오전 9시부터\n'
                  '### 이용규칙\n연체 시 대여 불가')
        units = source_units(source)
        self.assertEqual(len(units), 5)
        self.assertEqual([u['kinds'] for u in units],
                         [[], ['eligibility'], ['application'], ['benefit'], ['exclusion']])

    def test_short_draft_fits_conservative_budget_without_changing_source_or_schema(self):
        source = '지원내용\n아동 양육비 지원'
        args = (source, '한부모 지원', 'https://example.org', '', {})
        old, schema = build_policy_extraction_request(*args)
        compact, compact_schema = build_policy_extraction_request(*args, compact=True)
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(schema, compact_schema)
        self.assertLess(estimate_input_tokens([compact, schema_text]), 6000)
        self.assertLess(len(compact), len(old) / 2)
        self.assertIn(json.dumps(source, ensure_ascii=False), compact)
        self.assertTrue(inspect_ai_text(compact).allowed)

    def test_explicit_main_keeps_all_policy_conditions_and_excludes_outside_chrome(self):
        parsed = ai_classifier._parse_page_html(
            '<html><div>사이트 전체 안내 ' + '메뉴 ' * 3000 + 'outside@example.org</div>'
            '<main><h1>정책</h1><p>지원금 안내</p><p>중복 지원 불가</p><p>문의: inside@example.org</p></main></html>',
            'https://example.org')
        self.assertNotIn('outside@', parsed['draft_text'])
        self.assertIn('중복 지원 불가', parsed['draft_text'])
        self.assertIn('inside@', parsed['draft_text'])
        self.assertIn('email_address', inspect_ai_text(parsed['draft_text']).reasons)
        self.assertIn('outside@', parsed['policy_text'], 'Other workflows retain their source')

    def test_multiple_main_sections_are_not_silently_selected(self):
        parsed = ai_classifier._parse_page_html(
            '<main>첫 정책</main><main>둘째 정책 제외 조건</main>', 'https://example.org')
        self.assertIn('첫 정책', parsed['draft_text'])
        self.assertIn('제외 조건', parsed['draft_text'])


class DraftIncomeEndpoint(unittest.IsolatedAsyncioTestCase):
    async def preview(self, payload, *, content=SOURCE, title='한부모 지원'):
        source = {'text': content, 'title': title, 'content_type': 'text/html',
                  'final_url': 'https://example.org/policy'}
        with ExitStack() as stack:
            for p in (
                patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value=source['final_url'])),
                patch.object(notion_service, 'get_policy_db', return_value={'key': 'test', 'data_source_id': 'source'}),
                patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=False)),
                patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()),
                patch.object(api_server, '_policy_draft_target_schema', new=AsyncMock(return_value=SCHEMA)),
                patch.object(ai_classifier.aiohttp, 'ClientSession', _Session),
                patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=source)),
                patch.object(ScraperEngine, 'start', new=AsyncMock()),
                patch.object(ScraperEngine, 'fetch_rendered_text', new=AsyncMock(return_value=content)),
                patch.object(ScraperEngine, 'close', new=AsyncMock()),
                patch.object(ai_classifier, 'get_client', return_value=object()),
                patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=payload)),
                patch.object(policy_sync, 'normalize_draft_fields', side_effect=lambda *_args: None),
                patch.object(ai_coordinator, 'finish_job'),
                patch.object(policy_sync, 'release_maintenance_lock'),
            ):
                stack.enter_context(p)
            create = stack.enter_context(patch.object(notion_service, 'create_policy_page', new=AsyncMock()))
            try:
                return await api_server.policy_url_draft_preview_endpoint(api_server.PolicyUrlDraftPreviewReq(
                    url=source['final_url'], db_key='test'))
            finally:
                create.assert_not_awaited()

    async def test_preview_returns_source_and_review_separate_from_writable_fields(self):
        result = await self.preview(response())
        self.assertTrue(result['ok'])
        self.assertEqual(result['fields']['target_income'], INCOME)
        self.assertIn('draft_income_review_required', result['source_review']['reasons'])
        self.assertIn('target_income', result['source_review']['unverified_fields'])
        self.assertEqual(result['source_review']['source_text'], SOURCE)
        self.assertTrue(result['source_review']['required'])
        self.assertNotIn('source_review', result['fields'])

    async def test_title_matching_does_not_cut_sibling_conditions(self):
        content = ('### 도서 대여\n영유아를 위한 다양한 그림책을 자유롭게 열람하고 대여할 수 있는 공간입니다.\n'
                   '### 이용대상\n센터 유료회원\n### 이용규칙\n연체 시 대여 불가\n분실 시 변상 규정 적용')
        payload = {'support_detail': content, '_source_evidence': {'support_detail': content}}
        result = await self.preview(payload, content=content, title='도서 대여')
        self.assertEqual(result['source_review']['source_text'], content)
        self.assertIn('연체 시 대여 불가', result['fields']['support_detail'])

    async def test_evidence_hold_is_not_reported_as_temporary_ai_failure(self):
        with self.assertRaises(HTTPException) as error:
            await self.preview({'support_detail': '근거 없는 내용'})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail['error'], 'POLICY_DRAFT_REVIEW_REQUIRED')
        self.assertNotIn('다시 시도', error.exception.detail['message'])
        self.assertNotIn('기존 내용', error.exception.detail['message'])

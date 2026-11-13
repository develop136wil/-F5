"""URL로 정책 초안 만들기: 확인 전 무쓰기·중복·주소 안전성 회귀 테스트."""
import os
import json
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from google.genai import types

import ai_classifier
import api_server
from scraper_engine import ScraperEngine
from services import ai_coordinator
from services import notion_service as ns
from services import ocr_service
from services import policy_sync as ps


SCHEMA = {
    "_title": "복지F5 정책",
    "category": ["돌봄·양육", "생활지원"],
    "residence": ["전국", "도봉구"],
    "target_characteristics": ["일반", "한부모"],
    "life_cycle": ["영유아", "노년"],
}


class _Session:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Content:
    def __init__(self, body=b""):
        self.body = body

    async def read(self, _limit=-1):
        value = self.body if _limit < 0 else self.body[:_limit]
        self.body = self.body[len(value):]
        return value


class _Response:
    def __init__(self, status, url, *, headers=None, body=b""):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class TestPolicyUrlValidation(unittest.IsolatedAsyncioTestCase):
    async def test_private_or_local_address_is_rejected(self):
        for address in ("http://localhost/policy", "http://127.0.0.1/policy"):
            with self.subTest(address=address):
                with self.assertRaises(HTTPException) as error:
                    await api_server._validate_public_policy_url(address)
                self.assertEqual(error.exception.status_code, 400)

    async def test_public_address_is_normalized_without_fragment(self):
        answer = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch.object(api_server.socket, "getaddrinfo", return_value=answer):
            value = await api_server._validate_public_policy_url(
                "https://example.org/policy?id=7#section")
        self.assertEqual(value, "https://example.org/policy?id=7")

    async def test_excessively_long_address_is_rejected_before_dns(self):
        with patch.object(api_server.socket, "getaddrinfo") as resolve:
            with self.assertRaises(HTTPException) as error:
                await api_server._validate_public_policy_url(
                    "https://example.org/" + ("a" * 2050))
        self.assertEqual(error.exception.status_code, 400)
        resolve.assert_not_called()

    async def test_redirect_destination_is_validated_before_second_request(self):
        first = _Response(302, "https://public.example/start",
                          headers={"Location": "http://127.0.0.1/private"})
        session = Mock()
        session.get = Mock(return_value=first)

        async def validator(url):
            if "127.0.0.1" in url:
                raise ValueError("private destination")
            return url

        result = await ai_classifier.fetch_page_content_async(
            session, "https://public.example/start", url_validator=validator)
        self.assertEqual(result["text"], "")
        # 내부망 목적지는 검증에서 거부되어 실제 두 번째 GET이 일어나면 안 된다.
        self.assertEqual(session.get.call_count, 1)


class TestPolicyUrlDraftSanitizing(unittest.TestCase):
    def test_all_prose_fields_preserve_long_tail_conditions(self):
        prose_keys = ('support_detail', 'eligibility_detail', 'age_detail', 'target_income')
        content = '지원 조건 안내\n' + '원문 내용을 확인해주세요. ' * 390 + '\n마지막 조건: 중복 지원 제외\n'
        self.assertGreater(len(content), 5000)
        fields = api_server._sanitize_policy_draft_fields(
            dict.fromkeys(prose_keys, content), 'https://example.org/policy')
        for key in prose_keys:
            self.assertEqual(fields[key], content)
        properties = ns.build_policy_properties(fields)
        for key, label, kind in ns.get_policy_props():
            if key in prose_keys:
                joined = ''.join(part['text']['content'] for part in properties[label]['rich_text'])
                self.assertIn('마지막 조건: 중복 지원 제외', joined)

    def test_over_limit_text_is_rejected_not_silently_shortened(self):
        for key, limit in (('support_detail', 30000), ('eligibility_detail', 30000),
                           ('age_detail', 30000), ('target_income', 30000), ('contact', 5000)):
            with self.subTest(key=key), self.assertRaises(HTTPException) as raised:
                api_server._sanitize_policy_draft_fields({key: '가' * (limit + 1)},
                                                        'https://example.org/policy')
            self.assertEqual(raised.exception.status_code, 422)
            self.assertEqual(raised.exception.detail['field'], key)

    def test_untrusted_fields_are_removed_and_unknown_ages_remain_empty(self):
        fields = api_server._sanitize_policy_draft_fields({
            "title": "  아동 지원  ",
            "category": "돌봄·양육",
            "residence": ["도봉구", "도봉구", ""],
            "target_characteristics": "일반, 한부모",
            "start_age_months": None,
            "end_age_months": "",
            "server_only": "must disappear",
            "_source_urls": ["https://attacker.invalid"],
        }, "https://example.org/policy")
        self.assertEqual(fields["title"], "아동 지원")
        self.assertEqual(fields["residence"], ["도봉구"])
        self.assertEqual(fields["target_characteristics"], ["일반", "한부모"])
        self.assertIsNone(fields["start_age_months"])
        self.assertIsNone(fields["end_age_months"])
        self.assertNotIn("server_only", fields)
        self.assertEqual(fields["_source_urls"], ["https://example.org/policy"])


class TestPolicyUrlDraftEndpoints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ps.release_maintenance_lock()

    def tearDown(self):
        ps.release_maintenance_lock()

    async def test_document_preview_rejects_non_object_and_non_boolean_ocr(self):
        db = {'key':'custom','name':'시험','data_source_id':'source-id'}
        for response, expected in (([],503), ([{'is_relevant':True}],503),
                                   ({'is_relevant':'false'},503), ({'is_relevant':1},503),
                                   ({'is_relevant':False},422)):
            with self.subTest(response=response), \
                 patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value='https://example.org/policy.pdf')), \
                 patch.object(ns, 'get_policy_db', return_value=db), \
                 patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=False)), \
                 patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()), \
                 patch.object(api_server, '_policy_draft_target_schema', new=AsyncMock(return_value=SCHEMA)), \
                 patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock(return_value=response)), \
                 patch.object(ns, 'create_policy_page', new=AsyncMock()) as create:
                with self.assertRaises(HTTPException) as error:
                    await api_server.policy_url_draft_preview_endpoint(
                        api_server.PolicyUrlDraftPreviewReq(url='https://example.org/policy.pdf',db_key='custom',privacy_reviewed=True))
                self.assertEqual(error.exception.status_code, expected)
                create.assert_not_awaited()

    async def test_preview_never_creates_notion_page(self):
        db = {"key": "custom", "name": "복지F5 정책", "data_source_id": "source-id"}
        extracted = {
            "title": "도봉구 아동 돌봄",
            "category": "돌봄·양육",
            "residence": ["도봉구"],
            "target_characteristics": ["일반"],
            "life_cycle": ["영유아"],
            "support_detail": "돌봄 서비스를 지원합니다.",
        }
        source = {
            "text": "정책 상세 내용 " * 80,
            "title": "도봉구 아동 돌봄",
            "content_type": "text/html",
            "final_url": "https://example.org/policy",
        }
        create_page = AsyncMock()
        with patch.object(api_server, "_validate_public_policy_url", new=AsyncMock(
                return_value="https://example.org/policy")), \
             patch.object(ns, "get_policy_db", return_value=db), \
             patch.object(api_server.database, "run_db", new=AsyncMock(return_value=False)), \
             patch.object(api_server, "_acquire_policy_ai_job", new=AsyncMock()), \
             patch.object(api_server, "_policy_draft_target_schema", new=AsyncMock(return_value=SCHEMA)), \
             patch.object(ai_classifier.aiohttp, "ClientSession", _Session), \
             patch.object(ai_classifier, "fetch_page_content_async", new=AsyncMock(return_value=source)), \
             patch.object(ai_classifier, "get_client", return_value=object()), \
             patch.object(ai_classifier, "extract_policy_fields_async", new=AsyncMock(return_value=extracted)), \
             patch.object(ps, "normalize_draft_fields", side_effect=lambda *_args: None), \
             patch.object(ns, "create_policy_page", new=create_page):
            result = await api_server.policy_url_draft_preview_endpoint(
                api_server.PolicyUrlDraftPreviewReq(
                    url="https://example.org/policy", db_key="custom"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["fields"]["title"], "도봉구 아동 돌봄")
        self.assertEqual(result["options"]["residence"], ["전국", "도봉구"])
        create_page.assert_not_awaited()

    async def test_invalid_notion_template_is_rejected_before_ai_analysis(self):
        db = {"key": "custom", "name": "정책", "data_source_id": "source-id"}
        target = {"ok": True, "kind": "data_source", "id": "source-id",
                  "version": ns.DS_VERSION}
        with patch.object(ns, "get_credentials", return_value=("token", "")), \
             patch.object(ns.aiohttp, "ClientSession", _Session), \
             patch.object(ns, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ns, "get_policy_source_schema", new=AsyncMock(return_value={
                 "ok": True, "schema": SCHEMA,
                 "missing_properties": ["상세 지원 내용"], "type_mismatches": [],
             })), \
             patch.object(api_server.database, "run_db", new=AsyncMock()):
            with self.assertRaises(HTTPException) as error:
                await api_server._policy_draft_target_schema(db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("상세 지원 내용", error.exception.detail)

    async def test_duplicate_is_rejected_before_ai_job(self):
        db = {"key": "custom", "name": "정책", "data_source_id": "source-id"}
        acquire = AsyncMock()
        with patch.object(api_server, "_validate_public_policy_url", new=AsyncMock(
                return_value="https://example.org/policy")), \
             patch.object(ns, "get_policy_db", return_value=db), \
             patch.object(api_server.database, "run_db", new=AsyncMock(return_value=True)), \
             patch.object(api_server, "_acquire_policy_ai_job", new=acquire):
            with self.assertRaises(HTTPException) as error:
                await api_server.policy_url_draft_preview_endpoint(
                    api_server.PolicyUrlDraftPreviewReq(
                        url="https://example.org/policy", db_key="custom"))
        self.assertEqual(error.exception.status_code, 409)
        acquire.assert_not_awaited()

    async def test_failed_pdf_download_does_not_analyze_binary_as_web_text(self):
        db = {"key": "custom", "name": "정책", "data_source_id": "source-id"}
        source = {
            "text": "PDF-binary-placeholder" * 30,
            "title": "정책 문서",
            "content_type": "application/pdf",
            "final_url": "https://example.org/policy.pdf",
        }
        web_extract = AsyncMock()
        static_fetch = AsyncMock(return_value=source)
        with patch.object(api_server, "_validate_public_policy_url", new=AsyncMock(
                return_value="https://example.org/policy.pdf")), \
             patch.object(ns, "get_policy_db", return_value=db), \
             patch.object(api_server.database, "run_db", new=AsyncMock(return_value=False)), \
             patch.object(api_server, "_acquire_policy_ai_job", new=AsyncMock()), \
             patch.object(api_server, "_policy_draft_target_schema", new=AsyncMock(return_value=SCHEMA)), \
             patch.object(ai_classifier.aiohttp, "ClientSession", _Session), \
             patch.object(ai_classifier, "fetch_page_content_async", new=static_fetch), \
             patch.object(ocr_service, "analyze_image_or_pdf", new=AsyncMock(
                 return_value={"error": "Download failed"})), \
             patch.object(ai_classifier, "extract_policy_fields_async", new=web_extract):
            with self.assertRaises(HTTPException) as error:
                await api_server.policy_url_draft_preview_endpoint(
                    api_server.PolicyUrlDraftPreviewReq(
                        url="https://example.org/policy.pdf", db_key="custom", privacy_reviewed=True))
        self.assertEqual(error.exception.status_code, 422)
        static_fetch.assert_not_awaited()
        web_extract.assert_not_awaited()

    async def test_create_revalidates_and_forces_original_url(self):
        db = {"key": "custom", "name": "정책", "data_source_id": "source-id"}
        create = AsyncMock(return_value={"ok": True, "page_id": "page-1"})
        with patch.object(api_server, "_validate_public_policy_url", new=AsyncMock(
                return_value="https://example.org/policy")), \
             patch.object(ns, "get_policy_db", return_value=db), \
             patch.object(api_server, "_create_policy_draft", new=create):
            result = await api_server.policy_url_draft_create_endpoint(
                api_server.PolicyUrlDraftCreateReq(
                    url="https://example.org/policy#old",
                    db_key="custom",
                    source_review_confirmed=True,
                    fields={
                        "title": "확인한 정책",
                        "_source_urls": ["https://forged.invalid"],
                        "unknown": "drop me",
                    },
                ))
        self.assertTrue(result["ok"])
        passed_fields = create.await_args.args[1]
        self.assertEqual(passed_fields["_source_urls"], ["https://example.org/policy"])
        self.assertNotIn("unknown", passed_fields)

    async def test_create_is_locked_and_records_local_receipt(self):
        db = {"key": "custom", "name": "정책", "data_source_id": "source-id"}
        fields = {"title": "확인한 정책", "_source_urls": ["https://example.org/policy"]}
        run_db = AsyncMock(side_effect=[False, None])
        create_page = AsyncMock(return_value={"ok": True, "page_id": "page-1"})
        with patch.object(ps, "acquire_policy_mutation_lock", new=AsyncMock(return_value=True)), \
             patch.object(api_server.database, "run_db", new=run_db), \
             patch.object(api_server, "_policy_draft_state_db", new=AsyncMock(side_effect=[
                 None, {'claimed': True, 'attempt_id': 'attempt-1'}, None])), \
             patch.object(api_server, "_policy_draft_target_schema", new=AsyncMock(return_value=SCHEMA)), \
             patch.object(ps, "normalize_draft_fields", side_effect=lambda *_args: None), \
             patch.object(ns, "create_policy_page", new=create_page), \
             patch.object(ns, "page_deeplink", return_value="notion://page-1"):
            result = await api_server._create_policy_draft(db, fields)
        self.assertTrue(result["ok"])
        self.assertEqual(result["page_id"], "page-1")
        self.assertEqual(run_db.await_count, 2)
        self.assertIsNone(ps.maintenance_busy_message())


class TestPolicyDocumentTaxonomy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)

    async def test_document_prompt_uses_live_notion_options(self):
        from tests.test_ocr_evidence import candidate
        generate = Mock(return_value=object())
        client = Mock()
        client.aio.models.generate_content = generate
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        payload = candidate('문서 정책\n교육비 지원', title='문서 정책')
        payload['field_evidence']['title'] = ['문서 정책']
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason='STOP', content=types.Content(parts=[types.Part(
                text=json.dumps(payload, ensure_ascii=False))]))])
        taxonomy = {
            "category": ["기관 맞춤 분류"],
            "residence": ["새로운 지역"],
            "target_characteristics": ["기관 맞춤 대상"],
            "life_cycle": ["기관 맞춤 생애주기"],
        }
        with patch.object(ocr_service, "get_client", return_value=client), \
             patch.object(ocr_service, "download_content", new=AsyncMock(return_value=b"%PDF-test")), \
             patch.object(ai_coordinator, "acquire_model_slot", new=AsyncMock()), \
             patch.object(ai_coordinator, "await_api", new=AsyncMock(return_value=response)):
            result = await ocr_service.analyze_image_or_pdf(
                "https://example.org/policy.pdf", "application/pdf", taxonomy=taxonomy, privacy_reviewed=True)
        self.assertTrue(result["is_relevant"])
        prompt = generate.call_args.kwargs["contents"][0]
        for value in ("기관 맞춤 분류", "새로운 지역", "기관 맞춤 대상", "기관 맞춤 생애주기"):
            self.assertIn(value, prompt)
        self.assertNotIn("의료/재활", prompt)
        self.assertIn('target_income must be ""', prompt)
        self.assertIn('residence must be []', prompt)
        self.assertIn('Never write 전국', prompt)
        self.assertIn('Never write 소득 무관', prompt)


class TestRenderedUrlGuard(unittest.IsolatedAsyncioTestCase):
    async def test_browser_fallback_blocks_private_subrequest_and_closes_context(self):
        route = Mock()
        route.abort = AsyncMock()
        route.continue_ = AsyncMock()
        request = Mock(url="http://127.0.0.1/private")

        context = Mock()
        context.close = AsyncMock()

        async def register_route(_pattern, handler):
            context.handler = handler

        context.route = AsyncMock(side_effect=register_route)
        page = Mock()
        page.add_init_script = AsyncMock()

        async def goto(*_args, **_kwargs):
            await context.handler(route, request)
            raise RuntimeError("blocked navigation")

        page.goto = AsyncMock(side_effect=goto)
        context.new_page = AsyncMock(return_value=page)
        browser = Mock()
        browser.new_context = AsyncMock(return_value=context)
        engine = ScraperEngine()
        engine.browser = browser
        validator = AsyncMock(side_effect=HTTPException(status_code=400, detail="private"))

        text = await engine.fetch_rendered_text(
            "https://public.example/policy", url_validator=validator)
        self.assertEqual(text, "")
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()
        context.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

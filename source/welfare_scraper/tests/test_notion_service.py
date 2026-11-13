"""notion_service.py 순수 함수 단위 테스트."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import notion_service as ns


def _url_prop(url):
    return {"url": url}


class TestExtractPageFieldsSourceUrlDedup(unittest.TestCase):
    """(2026-08-05, GPT Codex 4차 리뷰로 발견) 관련 홈페이지 1·2·3에 같은 URL이 중복
    입력되면(실측: "희귀질환자 의료비 지원" — 전체 3개 중 2개만 고유) 중복 제거 없이
    그대로 _source_urls에 들어가, 이후 정책별 비교 단계에서 같은 원문이 AI 입력에
    두 번 병합될 수 있었다. 순서를 유지한 채 중복만 제거되는지 검증한다."""

    def test_duplicate_url_across_source_props_deduped(self):
        page = {
            "id": "page-dup",
            "properties": {
                "관련 홈페이지 1": _url_prop("https://a.example/policy"),
                "관련 홈페이지 2": _url_prop("https://a.example/policy"),  # 1번과 중복
                "관련 홈페이지 3": _url_prop("https://b.example/policy"),
            },
        }
        fields = ns.extract_page_fields(page)
        self.assertEqual(fields["_source_urls"],
                         ["https://a.example/policy", "https://b.example/policy"],
                         "중복 URL은 제거하되 최초 등장 순서는 유지해야 한다")

    def test_empty_url_props_excluded(self):
        page = {
            "id": "page-empty",
            "properties": {
                "관련 홈페이지 1": _url_prop(""),
                "관련 홈페이지 2": _url_prop("https://c.example/policy"),
                "관련 홈페이지 3": _url_prop(""),
            },
        }
        fields = ns.extract_page_fields(page)
        self.assertEqual(fields["_source_urls"], ["https://c.example/policy"])

    def test_all_distinct_urls_preserved(self):
        page = {
            "id": "page-distinct",
            "properties": {
                "관련 홈페이지 1": _url_prop("https://a.example"),
                "관련 홈페이지 2": _url_prop("https://b.example"),
                "관련 홈페이지 3": _url_prop("https://c.example"),
            },
        }
        fields = ns.extract_page_fields(page)
        self.assertEqual(fields["_source_urls"],
                         ["https://a.example", "https://b.example", "https://c.example"])


class TestEventGallerySchema(unittest.IsolatedAsyncioTestCase):
    async def test_required_news_gallery_properties_pass(self):
        properties = {
            "이름": {"type": "title"},
            "기관": {"type": "rich_text"},
            "선택": {"type": "select"},
            "기간": {"type": "date"},
        }
        with patch.object(ns, "_get", new=AsyncMock(return_value=(200, {"properties": properties}))):
            result = await ns.get_event_source_schema(
                object(), "token", {"kind": "data_source", "id": "source"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing_properties"], [])

    async def test_missing_and_wrong_types_are_reported(self):
        properties = {
            "이름": {"type": "rich_text"},
            "기관": {"type": "rich_text"},
            "기간": {"type": "date"},
        }
        with patch.object(ns, "_get", new=AsyncMock(return_value=(200, {"properties": properties}))):
            result = await ns.get_event_source_schema(
                object(), "token", {"kind": "database", "id": "database"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_properties"], ["선택"])
        self.assertEqual(result["type_mismatches"][0]["name"], "이름")


class TestPolicyUpdateResponseSafety(unittest.IsolatedAsyncioTestCase):
    async def update_with(self, status=200, payload=None, error=None):
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(return_value={} if payload is None else payload,
                                  side_effect=error)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.patch.return_value = response
        with patch.object(ns, 'get_credentials', return_value=('fake-token', '')), \
             patch.object(ns.aiohttp, 'ClientSession', return_value=session):
            result = await ns.update_page_properties('fake-page', {'문의처': {'rich_text': []}})
        session.patch.assert_called_once()
        return result

    async def test_timeout_and_malformed_success_are_uncertain_not_retried(self):
        for error in (TimeoutError('private transport details'), ValueError('malformed JSON')):
            with self.subTest(error=type(error).__name__):
                result = await self.update_with(error=error)
                self.assertEqual(result['error'], 'UPDATE_UNCERTAIN')
                self.assertNotIn('private', str(result))
        self.assertEqual((await self.update_with(payload=[]))['error'], 'UPDATE_UNCERTAIN')

    async def test_server_error_and_request_timeout_are_uncertain(self):
        for status in (408, 500, 502, 503):
            with self.subTest(status=status):
                result = await self.update_with(status, {'message': 'Remote response'})
                self.assertEqual(result['error'], 'UPDATE_UNCERTAIN')
                self.assertEqual(result['http_status'], status)

    async def test_definitive_400_preserves_rejection_even_with_invalid_body(self):
        result = await self.update_with(400, {'message': 'Invalid property'})
        self.assertEqual(result, {'ok': False, 'error': 'Invalid property', 'http_status': 400})
        result = await self.update_with(400, error=ValueError('bad body'))
        self.assertEqual(result, {'ok': False, 'error': 'HTTP 400', 'http_status': 400})

    async def test_transport_failure_before_response_returns_uncertain_code(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.patch.side_effect = TimeoutError('private transport details')
        with patch.object(ns, 'get_credentials', return_value=('fake-token', '')), \
             patch.object(ns.aiohttp, 'ClientSession', return_value=session):
            result = await ns.update_page_properties('fake-page', {'문의처': {'rich_text': []}})
        self.assertEqual(result['error'], 'UPDATE_UNCERTAIN')
        self.assertNotIn('private', str(result))
        session.patch.assert_called_once()

    async def test_success_is_acknowledged_once(self):
        self.assertEqual(await self.update_with(200, {'id': 'fake-page'}), {'ok': True})


if __name__ == '__main__':
    unittest.main()

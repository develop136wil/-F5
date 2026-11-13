"""First-use diagnostic: synthetic HTML/SDK, empty local state, no external I/O.

The incomplete-source test preserves warnings through the first preview.
Run only with scripts/run_isolated_tests.py.
"""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
import ai_classifier
import api_server
from services import notion_service, page_coverage
from tests.test_policy_url_draft import SCHEMA, _Response, _Session


URL = 'https://example.invalid/public-policy'
DB = {'key': 'blank-audit', 'name': '빈 정책표', 'data_source_id': 'synthetic-source'}


class BlankDraftSourceAudit(unittest.IsolatedAsyncioTestCase):
    async def preview(self, source):
        extract = AsyncMock(return_value={
            'title': '공개 지원 제도', 'support_detail': source['draft_text']})
        create = AsyncMock()
        with patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value=URL)), \
             patch.object(notion_service, 'get_policy_db', return_value=DB), \
             patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=False)), \
             patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()), \
             patch.object(api_server, '_policy_draft_target_schema', new=AsyncMock(return_value=SCHEMA)), \
             patch.object(ai_classifier.aiohttp, 'ClientSession', _Session), \
             patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=source)), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, 'extract_policy_fields_async', new=extract), \
             patch.object(notion_service, 'create_policy_page', new=create):
            result = await api_server.policy_url_draft_preview_endpoint(
                api_server.PolicyUrlDraftPreviewReq(url=URL, db_key=DB['key']))
        create.assert_not_awaited()
        return result, extract

    async def test_truncated_html_keeps_known_coverage_reason_in_first_preview(self):
        visible = '지원 안내문을 확인하고 신청 방법을 읽어주세요. ' * 20
        tail = '동일한 지원을 이미 받았다면 신청할 수 없습니다.'
        html = ('<html><head><title>공개 지원 제도</title></head><body><main>'
                '<h1>공개 지원 제도</h1><p>' + visible + '</p><style>' + 'x' * 70000
                + '</style><p>' + tail + '</p></main></body></html>')
        fake_session = Mock()
        fake_session.get.return_value = _Response(200, URL,
            headers={'Content-Type': 'text/html'}, body=html.encode('utf-8'))
        with patch.dict('os.environ', {'AI_FETCH_MAX_BYTES': '65536'}):
            source = await ai_classifier.fetch_page_content_async(fake_session, URL)
        self.assertIn('html_byte_limit', source['source_page']['reasons'])
        self.assertNotIn(tail, source['draft_text'])
        self.assertGreater(len(source['draft_text']), 300)
        result, extract = await self.preview(source)
        self.assertTrue(result['ok'])
        extract.assert_awaited_once()
        self.assertEqual(extract.await_args.kwargs['content'], source['draft_text'])
        self.assertIn('html_byte_limit', result['source_review']['reasons'])
        self.assertIn('페이지 용량 제한', result['source_review']['note'])
        self.assertTrue(result['source_review']['required'])
        self.assertFalse(result['source_review']['source_complete'])
        print('BLANK_DRAFT_PROBE partial_html: fetch_partial=1 preview_ok=1 specific_warning=1 notion_writes=0')

    async def test_complete_web_source_still_requires_user_review_without_write(self):
        content = '공개 지원 제도\n' + '신청 방법을 원문에서 확인해주세요. ' * 20
        result, extract = await self.preview({'draft_text': content, 'text': content,
            'title': '공개 지원 제도', 'content_type': 'text/html', 'final_url': URL,
            'source_page': page_coverage.make()})
        self.assertTrue(result['ok'])
        self.assertTrue(result['source_review']['required'])
        self.assertEqual(result['fields']['_source_urls'], [URL])
        extract.assert_awaited_once()

    async def test_first_registration_cannot_skip_source_confirmation(self):
        create = AsyncMock()
        with patch.object(api_server, '_create_policy_draft', new=create):
            with self.assertRaises(HTTPException) as raised:
                await api_server.policy_url_draft_create_endpoint(
                    api_server.PolicyUrlDraftCreateReq(url=URL, db_key=DB['key'],
                        fields={'title': '공개 지원 제도', 'support_detail': '교육비 지원'}))
        self.assertEqual(raised.exception.status_code, 409)
        create.assert_not_awaited()

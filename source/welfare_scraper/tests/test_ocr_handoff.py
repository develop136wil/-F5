import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from google.genai import types

from bs4 import BeautifulSoup

from services import news_attachments as attachments
from services import ocr_service, ai_coordinator
from services.ocr_payload import OcrPayload
from tests.test_ocr_evidence import candidate as ocr_candidate


PDF = b'%PDF-1.4 mock-scanned-document'
URL = 'https://example.org/scan.pdf'


class TestPayloadBudget(unittest.TestCase):
    def test_capacity_and_idempotent_release(self):
        with patch.object(OcrPayload, 'MAX_BYTES', 10), patch.object(OcrPayload, 'MAX_ITEMS', 2):
            first = OcrPayload.retain(URL, b'12345')
            second = OcrPayload.retain(URL, b'67890')
            try:
                self.assertIsNone(OcrPayload.retain(URL, b'1'))
                self.assertEqual(OcrPayload._bytes, 10)
                first.close()
                first.close()
                third = OcrPayload.retain(URL, b'1')
                self.assertIsNotNone(third)
                third.close()
            finally:
                first.close()
                second.close()
        self.assertEqual((OcrPayload._bytes, OcrPayload._items), (0, 0))


class TestDocumentQuality(unittest.TestCase):
    def test_navigation_file_is_not_an_article_attachment(self):
        soup = BeautifulSoup('''<header><a href="/common.hwp">공통 서식</a></header>
            <nav><a href="/guide.pdf">메뉴</a></nav>
            <article><header><a href="/real.hwpx">사업 안내</a></header></article>''', 'html.parser')
        found = attachments.discover_attachments(soup, 'https://example.org/post')
        self.assertEqual([entry['url'] for entry in found], ['https://example.org/real.hwpx'])

    def test_damaged_font_mapping_is_not_accepted_based_on_length(self):
        self.assertFalse(attachments.pdf_text_is_readable('지원 안내 \x01\x0f\ufffd ' * 30))
        self.assertFalse(attachments.pdf_text_is_readable('(cid:123) ' * 30))
        self.assertTrue(attachments.pdf_text_is_readable('가족 지원 대상과 신청 방법을 안내합니다. ' * 20))
        self.assertTrue(attachments.pdf_text_is_readable('Application details for welfare support. ' * 20))


class TestEnrichmentHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_html_keeps_attachment_link_in_last_chunk(self):
        import ai_classifier
        response = MagicMock()
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        response.headers, response.charset, response.url = {}, 'utf-8', URL
        response.content.read = AsyncMock(side_effect=[
            b'<html><body><p>Article</p>',
            b'<a href="/guide.pdf">PDF</a></body></html>', b'',
        ])
        result = await ai_classifier.fetch_page_content_async(Mock(get=Mock(return_value=response)), URL)
        self.assertEqual(result['attachments'], ['https://example.org/guide.pdf'])
        self.assertEqual(response.content.read.await_count, 3)

    async def test_chunked_html_keeps_existing_size_cap(self):
        import ai_classifier
        response = MagicMock()
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        response.headers, response.charset, response.url = {}, 'utf-8', URL
        response.content.read = AsyncMock(side_effect=[b'x' * 65536, b'z'])
        with patch.dict('os.environ', {'AI_FETCH_MAX_BYTES': '65536'}):
            result = await ai_classifier.fetch_page_content_async(Mock(get=Mock(return_value=response)), URL)
        self.assertEqual(response.content.read.await_count, 2)
        self.assertNotIn('z', result['text'])

    async def test_profile_default_does_not_retain_bytes_but_legacy_can_opt_in(self):
        downloader = AsyncMock(return_value={'url': URL, 'kind': 'pdf', 'data': PDF, 'sha256': 'a' * 64})
        page = {'text': '', 'attachments': [URL]}
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=attachments.DocumentText())):
            profile = await attachments.enrich_page_locally(None, page, downloader=downloader)
            legacy = await attachments.enrich_page_locally(None, page, downloader=downloader,
                                                          retain_pdf_for_ocr=True)
        self.assertNotIn('_ocr_payload', profile)
        payload = legacy.pop('_ocr_payload')
        try:
            self.assertIs(payload.data, PDF)
            json.dumps(legacy)  # All public metadata stays serializable.
        finally:
            payload.close()

    async def test_cancellation_while_reading_next_attachment_releases_first_pdf(self):
        calls = 0

        async def downloader(_session, _item):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError()
            return {'url': URL, 'kind': 'pdf', 'data': PDF, 'sha256': 'a' * 64}

        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=attachments.DocumentText())):
            with self.assertRaises(asyncio.CancelledError):
                await attachments.enrich_page_locally(None, {'text': '', 'attachments': [URL, URL + '?v=2']},
                    downloader=downloader, retain_pdf_for_ocr=True)
        self.assertEqual(OcrPayload._items, 0)

    async def test_garbled_pdf_text_is_removed_and_marked_for_ocr(self):
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(side_effect=[
                attachments.DocumentText('잘못된 문자 \x01 ' * 40),
                attachments.DocumentText(reasons=['ocr_models_unavailable'])])) as worker:
            result = await attachments._extract_downloaded({'kind': 'pdf', 'data': PDF})
        self.assertEqual(result['status'], 'ocr_required')
        self.assertEqual(result['text'], '')
        self.assertEqual([call.args[1] for call in worker.await_args_list], ['pdf_text', 'pdf_ocr'])


class TestOcrBytes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)

    async def test_bytes_and_url_keep_same_model_prompt_and_rate_admission(self):
        client = Mock()
        client.aio.models.generate_content = Mock(return_value=object())
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason='STOP', content=types.Content(parts=[types.Part(
                text=json.dumps(ocr_candidate('교육비 지원'), ensure_ascii=False))]))])
        with patch.object(ocr_service, 'get_client', return_value=client), \
             patch.object(ocr_service, 'download_content', new=AsyncMock(return_value=PDF)) as download, \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire, \
             patch.object(ai_coordinator, 'await_api', new=AsyncMock(return_value=response)):
            a = await ocr_service.analyze_image_or_pdf(URL, privacy_reviewed=True)
            b = await ocr_service.analyze_downloaded_document(PDF, privacy_reviewed=True)
        self.assertEqual(a, b)
        self.assertTrue(a['is_relevant'])
        self.assertEqual(a['support_detail'], '교육비 지원')
        self.assertTrue(a['needs_source_review'])
        self.assertEqual(download.await_count, 1)
        self.assertEqual(acquire.await_count, 2)
        first, second = client.aio.models.generate_content.call_args_list
        self.assertEqual(first.kwargs, second.kwargs)

    async def test_oversized_and_invalid_payloads_never_consume_ai_slot(self):
        with patch.object(ocr_service, 'get_client', return_value=Mock()), \
             patch.object(ocr_service, 'max_ocr_bytes', return_value=10), \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire:
            for data in (PDF, b'<html>', b'', 'not bytes'):
                with self.subTest(data=data):
                    result = await ocr_service.analyze_downloaded_document(data, privacy_reviewed=True)
                    self.assertFalse(result['is_relevant'])
        acquire.assert_not_awaited()


class TestLegacyHandoff(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, cached=None, cancel_ocr=False, retain=True, image_count=0, image_success=True,
                       real_review_gate=False):
        import ai_classifier
        import history_manager
        from services.scraper_service import ScraperService
        payload = OcrPayload.retain(URL, PDF) if retain else None
        page = {'text': '사업 설명 ' * 120, 'images': [], 'attachments': [URL],
                'attachment_reports': [{'url': URL, 'kind': 'pdf', 'status': 'ocr_required'}],
                'attachment_fingerprint': 'a' * 64}
        for index in range(image_count):
            image_url = f'https://example.org/poster-{index}.jpg'
            page['attachments'].append(image_url)
            page['attachment_reports'].append({'url': image_url, 'kind': 'jpg', 'status': 'ocr_required'})
        if payload:
            page['_ocr_payload'] = payload
        service = ScraperService(headless=True)
        service.ocr_rate_limiter = Mock(acquire=AsyncMock())
        service.ai_rate_limiter = Mock(acquire=AsyncMock())
        analysis = {'is_relevant': True, 'category': '복지', 'title': '지원 안내'}
        bytes_ocr = (AsyncMock(wraps=ocr_service.analyze_downloaded_document) if real_review_gate else
                     AsyncMock(side_effect=asyncio.CancelledError() if cancel_ocr else None, return_value=analysis))
        url_ocr_mock = (AsyncMock(wraps=ocr_service.analyze_image_or_pdf) if real_review_gate else
                        AsyncMock(return_value=analysis if image_success else {'is_relevant': False}))
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value={})), \
             patch.object(attachments, 'enrich_page_locally', new=AsyncMock(return_value=page)), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=cached), \
             patch.object(ai_classifier, 'classify_post_async', new=AsyncMock(return_value=analysis.copy())), \
             patch.object(ocr_service, 'analyze_downloaded_document', new=bytes_ocr), \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=url_ocr_mock) as url_ocr:
            try:
                result = await service._process_single_item_ai(None, None, {'title': '지원 안내', 'link': URL})
            finally:
                if payload:
                    self.assertIsNone(payload.data)
                self.assertEqual(OcrPayload._items, 0)
        json.dumps(result)
        return result, bytes_ocr.await_count, url_ocr.await_count

    async def test_retained_pdf_avoids_second_download_and_never_reaches_db(self):
        result, buffered, downloaded = await self.exercise()
        self.assertEqual((buffered, downloaded), (1, 0))
        self.assertNotIn('_ocr_payload', result)
        self.assertEqual(json.loads(result['ai_data'])['source_attachments']['status'], 'complete')

    async def test_budget_fallback_keeps_old_url_path(self):
        _, buffered, downloaded = await self.exercise(retain=False)
        self.assertEqual((buffered, downloaded), (0, 1))

    async def test_pdf_and_attachment_only_image_are_both_read_with_long_html(self):
        result, buffered, downloaded = await self.exercise(image_count=2)
        self.assertEqual((buffered, downloaded), (1, 1))
        state = json.loads(result['ai_data'])['source_attachments']
        self.assertEqual([f['status'] for f in state['files']], ['ocr_complete', 'ocr_complete', 'ocr_required'])
        self.assertEqual(state['status'], 'review_required')

    async def test_unsuccessful_image_does_not_claim_completion(self):
        result, buffered, downloaded = await self.exercise(image_count=1, image_success=False)
        self.assertEqual((buffered, downloaded), (1, 1))
        self.assertEqual(json.loads(result['ai_data'])['source_attachments']['files'][1]['status'], 'ocr_failed')

    async def test_real_document_review_gate_survives_legacy_summary_and_candidate_api(self):
        import api_server
        from services import notion_service
        with patch.object(ocr_service, 'get_client', side_effect=AssertionError('No OCR SDK client')) as client, \
             patch.object(ocr_service, 'download_content', new=AsyncMock()) as download, \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as slot:
            result, buffered, downloaded = await self.exercise(image_count=1, real_review_gate=True)
            self.assertEqual((buffered, downloaded), (1, 1))
            client.assert_not_called()
            download.assert_not_awaited()
            slot.assert_not_awaited()
        state = json.loads(result['ai_data'])['source_attachments']
        self.assertEqual(state['status'], 'review_required')
        self.assertEqual(state['analyzed'], 0)
        self.assertEqual([item['status'] for item in state['files']], ['ai_transmission_held', 'ai_transmission_held'])
        self.assertEqual([item['url'] for item in state['files']], [URL, 'https://example.org/poster-0.jpg'])
        self.assertTrue(all(item['label'] == '원본 파일 직접 확인 필요 — AI 전송 보류' for item in state['files']))
        self.assertNotIn('이미지 분석 실패', state['note'])
        # Feed the exact serialized legacy result through the actual API mapper.
        # DB reads alone are fake; no operational DB or Notion call occurs.
        row = {**result, 'id': 101, 'site_name': '시험 기관', 'manual_news': True,
               'event_date': '2999-01-01'}
        with patch.object(api_server.database, 'run_db', new=AsyncMock(side_effect=[[row], set()])), \
             patch.object(notion_service, 'is_configured', return_value=False):
            response = await api_server.notion_candidates()
        self.assertEqual(response['count'], 1)
        candidate = response['items'][0]
        self.assertEqual(candidate['link'], URL)
        self.assertTrue(candidate['attachment_review_required'])
        self.assertIn('원본 파일 직접 확인 필요 — AI 전송 보류', candidate['attachment_note'])
        self.assertNotIn('이미지 분석 실패', candidate['attachment_note'])

    async def test_legacy_url_pdf_without_buffer_keeps_intentional_hold_label(self):
        with patch.object(ocr_service, 'get_client') as client, \
             patch.object(ocr_service, 'download_content', new=AsyncMock()) as download:
            result, buffered, downloaded = await self.exercise(retain=False, real_review_gate=True)
        self.assertEqual((buffered, downloaded), (0, 1))
        state = json.loads(result['ai_data'])['source_attachments']
        self.assertEqual(state['files'][0]['status'], 'ai_transmission_held')
        self.assertEqual(state['files'][0]['url'], URL)
        client.assert_not_called()
        download.assert_not_awaited()

    async def test_cache_hit_releases_retained_bytes_without_ocr(self):
        _, buffered, downloaded = await self.exercise(
            cached={'is_relevant': True, 'category': '복지', 'title': '지원 안내'})
        self.assertEqual((buffered, downloaded), (0, 0))

    async def test_incomplete_cache_is_reanalyzed_and_releases_retained_bytes(self):
        _, buffered, downloaded = await self.exercise(
            cached={'is_relevant': True, 'title': '지원 안내'})
        self.assertEqual((buffered, downloaded), (1, 0))

    async def test_cancelled_ocr_releases_retained_bytes(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.exercise(cancel_ocr=True)

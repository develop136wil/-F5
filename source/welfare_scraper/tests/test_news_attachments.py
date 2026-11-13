import io
import json
import asyncio
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bs4 import BeautifulSoup
from google.genai import types

from services import news_attachments as attachments


def make_hwpx(*texts):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<hp:section xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
           + ''.join(f'<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>' for text in texts)
           + '</hp:section>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Contents/section0.xml', xml)
    return output.getvalue()


class TestAttachmentDiscovery(unittest.TestCase):
    def discover(self, html):
        return attachments.discover_attachments(
            BeautifulSoup(html, 'html.parser'), 'https://example.org/board/view?id=1')

    def test_direct_query_download_and_declared_filename_are_detected(self):
        items = self.discover('''
            <a href="/files/GUIDE.PDF?download=1">안내서</a>
            <a href="/fileDown.do?atchFileId=7&fileSn=1">붙임 신청서.hwp</a>
            <a href="/download?id=9" download="사업 안내.hwpx">받기</a>
            <a href="/fileDownload.do?fileId=10">첨부파일 다운로드</a>
            <a href="/board/list">파일 목록</a>
        ''')
        self.assertEqual([item['kind'] for item in items], ['pdf', 'hwp', 'hwpx', ''])
        self.assertEqual(items[0]['url'], 'https://example.org/files/GUIDE.PDF?download=1')
        self.assertEqual(items[2]['filename'], '사업 안내.hwpx')

    def test_duplicate_and_non_http_links_are_ignored(self):
        items = self.discover('''
            <a href="javascript:download('x.pdf')">자바스크립트</a>
            <a href="/a.pdf">PDF</a><a href="/a.pdf">같은 PDF</a>
        ''')
        self.assertEqual(len(items), 1)


class TestLocalExtraction(unittest.IsolatedAsyncioTestCase):
    def test_hwpx_text_is_extracted_without_ai(self):
        data = make_hwpx('한부모 가족 지원', '신청 기간 2026년 9월')
        text = attachments.extract_hwpx_text(data)
        self.assertIn('한부모 가족 지원', text)
        self.assertIn('신청 기간', text)

    def test_zip_bomb_sized_hwpx_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('Contents/section0.xml', b'x' * (51 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, '제한'):
            attachments.extract_hwpx_text(output.getvalue())

    async def test_enrichment_records_text_hash_and_unreadable_hwp_notice(self):
        hwpx = make_hwpx('노년 돌봄 지원 상세 내용')

        async def downloader(_session, item):
            if item['kind'] == 'hwp':
                return dict(item, final_url=item['url'], data=b'synthetic HWP', sha256='b' * 64)
            return dict(item, final_url=item['url'], filename='안내.hwpx', kind='hwpx',
                        content_type='application/hwp+zip', data=hwpx,
                        sha256='a' * 64)

        page = {
            'text': '게시판 본문',
            'attachment_items': [
                {'url': 'https://example.org/1', 'filename': '안내.hwpx', 'label': '', 'kind': 'hwpx'},
                {'url': 'https://example.org/2', 'filename': '신청서.hwp', 'label': '', 'kind': 'hwp'},
            ],
        }
        failed = attachments.DocumentText(reasons=['file_read_error'])
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=failed)) as worker:
            result = await attachments.enrich_page_locally(None, page, downloader=downloader)
        worker.assert_awaited_once_with(b'synthetic HWP', 'hwp')
        merged = attachments.merge_page_text(result)
        self.assertIn('노년 돌봄 지원 상세 내용', merged)
        self.assertIn('안전하게 읽을 수 없는 문서', merged)
        self.assertTrue(result['attachment_fingerprint'])
        self.assertEqual([r['status'] for r in result['attachment_reports']],
                         ['text_extracted', 'partial_extracted'])

    async def test_private_literal_attachment_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '내부망|예약'):
            await attachments.validate_public_attachment_url('http://127.0.0.1/file.pdf')

    async def test_image_only_post_reads_locally_and_keeps_method_without_unread_notice(self):
        from services import ocr_service
        downloader = AsyncMock(return_value={'kind': 'png', 'data': b'synthetic PNG', 'sha256': 'a' * 64})
        extracted = attachments.DocumentText('합성 포스터 지원 내용', ['ocr_verification_required'], 1, 1)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker, \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as cloud:
            result = await attachments.enrich_page_locally(None, {
                'text': '포스터를 참고하세요.', 'images': ['https://example.org/viewImage?id=1'],
            }, downloader=downloader)
        self.assertEqual(result['attachment_reports'][0]['status'], 'ocr_text_extracted')
        self.assertIn('합성 포스터 지원 내용', attachments.merge_page_text(result))
        self.assertEqual(result['attachment_notices'], [])
        self.assertIn('ocr_verification_required', result['attachment_reports'][0]['coverage']['reasons'])
        self.assertEqual(attachments.summarize_attachment_reports(result['attachment_reports'])['analyzed'], 1)
        downloader.assert_awaited_once()
        worker.assert_awaited_once_with(b'synthetic PNG', 'image')
        cloud.assert_not_awaited()

    async def test_hwp_readable_text_is_merged_before_body_without_cloud_upload(self):
        from services import ocr_service
        extracted = attachments.DocumentText('한글 문서의 지원 대상과 신청 방법', [], 1, 1)
        downloader = AsyncMock(return_value={'kind': 'hwp', 'filename': '지원 안내.hwp',
                                             'data': b'synthetic HWP', 'sha256': 'c' * 64})
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker, \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as cloud:
            page = await attachments.enrich_page_locally(None, {'text': '게시글 본문',
                'attachments': ['https://example.invalid/guide.hwp']}, downloader=downloader)
        merged = attachments.merge_page_text(page)
        self.assertLess(merged.index(extracted.text), merged.index('게시글 본문'))
        self.assertEqual(page['attachment_reports'][0]['status'], 'text_extracted')
        self.assertEqual(page['attachment_notices'], [])
        worker.assert_awaited_once_with(b'synthetic HWP', 'hwp')
        cloud.assert_not_awaited()

    async def test_ocr_failure_keeps_link_for_review_without_cloud_fallback(self):
        from services import ocr_service
        downloader = AsyncMock(return_value={'kind': 'png', 'data': b'synthetic PNG'})
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=
                attachments.DocumentText(reasons=['ocr_models_unavailable']))) as worker, \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as cloud:
            page = await attachments.enrich_page_locally(None, {
                'images': ['https://example.invalid/poster.png'], 'text': ''}, downloader=downloader)
        self.assertEqual(page['attachment_reports'][0]['status'], 'ocr_required')
        self.assertEqual(page['attachments'], ['https://example.invalid/poster.png'])
        self.assertIn('모델을 사용할 수 없음', attachments.merge_page_text(page))
        self.assertEqual(page['attachment_text'], '')
        worker.assert_awaited_once()
        cloud.assert_not_awaited()

    async def test_long_body_decorative_image_does_not_trigger_attachment_warning(self):
        result = await attachments.enrich_page_locally(None, {
            'text': '지원 내용 ' * 200, 'images': ['https://example.org/logo.png'],
        })
        self.assertEqual(result['attachment_reports'], [])

    async def test_attachment_enrichment_has_two_page_concurrency_limit(self):
        await self._assert_two_page_concurrency({'text': '', 'attachments': ['https://example.org/a.hwpx']})

    async def test_image_only_posts_share_the_two_page_concurrency_limit(self):
        await self._assert_two_page_concurrency({'text': '', 'images': ['https://example.org/poster.png']})

    async def _assert_two_page_concurrency(self, page):
        reached_two = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0
        data = make_hwpx('지원 내용')

        async def downloader(_session, item):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                reached_two.set()
            try:
                await release.wait()
                return dict(item, kind='hwpx', data=data, sha256='a' * 64)
            finally:
                active -= 1

        tasks = [asyncio.create_task(attachments.enrich_page_locally(None, page, downloader=downloader))
                 for _ in range(3)]
        try:
            await asyncio.wait_for(reached_two.wait(), timeout=2)
            self.assertEqual(maximum, 2)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)

    def test_html_response_with_pdf_filename_is_not_treated_as_pdf(self):
        self.assertEqual(attachments._kind_from_content(
            'text/html', b'<html>Please log in</html>', 'guide.pdf'), '')

    def test_summary_keeps_partial_analysis_visible(self):
        summary = attachments.summarize_attachment_reports([
            {'filename': '안내.pdf', 'kind': 'pdf', 'status': 'text_extracted'},
            {'filename': '신청서.hwp', 'kind': 'hwp', 'status': 'manual_review'},
        ])
        self.assertEqual((summary['count'], summary['analyzed'], summary['status']),
                         (2, 1, 'review_required'))
        self.assertIn('신청서.hwp', summary['note'])

    def test_text_pdf_is_extracted_locally_when_pypdf_is_available(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        except ImportError:
            self.skipTest('pypdf not installed')
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 100 Td (Welfare F5 local PDF extraction) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        self.assertIn('Welfare F5 local PDF extraction', attachments.extract_pdf_text(output.getvalue()))


class Response:
    def __init__(self, status=200, headers=None, chunks=(), url='https://example.org/download'):
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.content = SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b'']))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class TestAttachmentDownload(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_validation_is_bounded_by_whole_download_timeout(self):
        never_ready = asyncio.Event()
        session = SimpleNamespace(get=Mock())

        async def validator(_value):
            await never_ready.wait()

        with patch.object(attachments, 'DOWNLOAD_TIMEOUT_SECONDS', 0.02):
            with self.assertRaises(TimeoutError):
                await attachments.download_attachment(session, {'url': 'https://example.org/guide.pdf'},
                                                      url_validator=validator)
        session.get.assert_not_called()

    async def test_oversized_declared_file_is_rejected_before_reading(self):
        response = Response(headers={'Content-Length': str(21 * 1024 * 1024)})
        session = SimpleNamespace(get=Mock(return_value=response))
        with self.assertRaisesRegex(ValueError, '용량'):
            await attachments.download_attachment(session, {'url': 'https://example.org/guide.pdf'},
                url_validator=AsyncMock(side_effect=lambda value: value), max_bytes=20 * 1024 * 1024)
        response.content.read.assert_not_awaited()

    async def test_chunked_download_is_read_to_eof_and_identified(self):
        data = make_hwpx('파일 내용')
        response = Response(headers={
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': 'attachment; filename="guide.hwpx"',
        }, chunks=(data[:20], data[20:]))
        session = SimpleNamespace(get=Mock(return_value=response))
        validator = AsyncMock(side_effect=lambda value: value)
        result = await attachments.download_attachment(session,
            {'url': 'https://example.org/download?id=1', 'filename': '', 'label': '첨부파일', 'kind': ''},
            url_validator=validator)
        self.assertEqual(result['data'], data)
        self.assertEqual(result['kind'], 'hwpx')
        self.assertEqual(response.content.read.await_count, 3)

    async def test_redirect_is_revalidated_before_private_request(self):
        response = Response(status=302, headers={'Location': 'http://127.0.0.1/file.pdf'})
        session = SimpleNamespace(get=Mock(return_value=response))

        async def validator(value):
            if '127.0.0.1' in value:
                raise ValueError('private destination')
            return value

        with self.assertRaisesRegex(ValueError, 'private'):
            await attachments.download_attachment(session, {'url': 'https://example.org/download'},
                                                  url_validator=validator)
        self.assertEqual(session.get.call_count, 1)


class TestLegacyAttachmentResult(unittest.IsolatedAsyncioTestCase):
    async def test_review_state_is_saved_in_ai_data_without_schema_change(self):
        import ai_classifier
        import history_manager
        from services.scraper_service import ScraperService
        page = {
            'text': '지원 대상과 신청 내용을 확인하는 안내 게시글입니다. ' * 30,
            'images': [], 'attachments': ['https://example.org/form.hwp'],
            'attachment_items': [{'url': 'https://example.org/form.hwp', 'kind': 'hwp',
                                  'filename': '신청서.hwp', 'label': ''}],
        }
        service = ScraperService(headless=True)
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=page)), \
             patch.object(ai_classifier, 'classify_post_async', new=AsyncMock(return_value={
                 'is_relevant': True, 'category': '복지', 'target': '일반'})), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None):
            result = await service._process_single_item_ai(None, None,
                {'title': '지원 안내', 'link': 'https://example.org/post', 'site_name': '기관'})
        analysis = json.loads(result['ai_data'])
        self.assertEqual(analysis['source_attachments']['status'], 'review_required')
        self.assertIn('신청서.hwp', analysis['source_attachments']['note'])

    async def test_hwp_only_page_does_not_waste_screenshot_ai_call(self):
        import ai_classifier
        import history_manager
        from services.scraper_service import ScraperService
        service = ScraperService(headless=True)
        scraper = SimpleNamespace(fetch_rendered_text=AsyncMock(return_value=''),
                                  capture_page_vision=AsyncMock())
        page = {'text': '첨부 확인', 'images': [], 'attachments': ['https://example.org/guide.hwp']}
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=page)), \
             patch.object(ai_classifier, 'classify_post_async', new=AsyncMock(return_value={
                 'is_relevant': True, 'category': '복지', 'target': '일반'})), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None):
            await service._process_single_item_ai(None, None,
                {'title': '지원 안내', 'link': 'https://example.org/post', 'site_name': '기관'}, scraper)
        scraper.capture_page_vision.assert_not_awaited()


class TestOcrAttachmentSafety(unittest.IsolatedAsyncioTestCase):
    async def test_query_image_uses_actual_png_mime(self):
        import json
        from tests.test_ocr_evidence import candidate
        from services import ocr_service, ai_coordinator, ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        client = Mock()
        client.aio.models.generate_content = Mock(return_value=object())
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        payload = candidate('포스터\n교육비 지원', title='포스터')
        payload['field_evidence']['title'] = ['포스터']
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason='STOP', content=types.Content(parts=[types.Part(
                text=json.dumps(payload, ensure_ascii=False))]))])
        with patch.object(ocr_service, 'get_client', return_value=client), \
             patch.object(ocr_service, 'download_content', new=AsyncMock(return_value=b'\x89PNG\r\n\x1a\nimage')), \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()), \
             patch.object(ai_coordinator, 'await_api', new=AsyncMock(return_value=response)):
            result = await ocr_service.analyze_image_or_pdf('https://example.org/fileDown.do?id=1', privacy_reviewed=True)
        self.assertTrue(result['is_relevant'])
        part = client.aio.models.generate_content.call_args.kwargs['contents'][1]
        self.assertEqual(part.inline_data.mime_type, 'image/png')

    async def test_html_instead_of_pdf_does_not_consume_ai_slot(self):
        from services import ocr_service, ai_coordinator
        client = Mock()
        with patch.object(ocr_service, 'get_client', return_value=client), \
             patch.object(ocr_service, 'download_content', new=AsyncMock(return_value=b'<html>Login</html>')), \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire:
            result = await ocr_service.analyze_image_or_pdf('https://example.org/guide.pdf', privacy_reviewed=True)
        self.assertFalse(result['is_relevant'])
        acquire.assert_not_awaited()
        client.aio.models.generate_content.assert_not_called()


if __name__ == '__main__':
    unittest.main()

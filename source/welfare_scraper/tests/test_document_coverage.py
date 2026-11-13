import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import AsyncMock, Mock, patch

from services import document_text as documents
from services import news_attachments as attachments
from services.attachment_cache import AttachmentTextCache


def page(text='', *, blank=False, error=False, stream_size=2):
    return Mock(get_contents=Mock(return_value=None if blank else Mock(get_data=Mock(return_value=b'x' * stream_size))),
                extract_text=Mock(side_effect=ValueError('broken page') if error else None, return_value=text))


def hwpx(sections, preview=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index, xml in enumerate(sections):
            archive.writestr(f'Contents/section{index}.xml', xml)
        if preview is not None:
            archive.writestr('Preview/PrvText.txt', preview)
    return output.getvalue()


class TestPdfCoverage(unittest.TestCase):
    def extract(self, pages, **kwargs):
        with patch('pypdf.PdfReader', return_value=Mock(is_encrypted=False, pages=pages)):
            return documents.extract_pdf_document(b'%PDF-test', **kwargs)

    def test_complete_pages_and_blank_page_have_no_partial_warning(self):
        result = self.extract([page('first page'), page(blank=True), page('last page')])
        self.assertEqual(result.text, 'first page last page')
        self.assertEqual(result.reasons, [])
        self.assertEqual((result.units_total, result.units_examined), (3, 3))

    def test_page_limit_preserves_read_prefix_and_reports_omitted_pages(self):
        pages = [page('first page'), page('second page'), page('not read')]
        result = self.extract(pages, max_pages=2)
        self.assertEqual(result.text, 'first page second page')
        self.assertEqual(result.reasons, ['page_limit'])
        self.assertEqual(result.units_examined, 2)
        pages[2].extract_text.assert_not_called()

    def test_character_limit_inside_one_page_is_visible(self):
        result = self.extract([page('abcdefghijk')], max_chars=10)
        self.assertEqual(result.text, 'abcdefghij')
        self.assertIn('char_limit', result.reasons)

    def test_exact_limit_is_not_partial_unless_more_text_exists(self):
        exact = self.extract([page('abcdefghij'), page(blank=True)], max_chars=10)
        self.assertEqual(exact.reasons, [])
        extra = self.extract([page('abcdefghij'), page('extra')], max_chars=10)
        self.assertIn('char_limit', extra.reasons)

    def test_unreadable_middle_page_does_not_drop_other_pages(self):
        result = self.extract([page('before'), page(error=True), page('after')])
        self.assertEqual(result.text, 'before after')
        self.assertIn('page_read_error', result.reasons)

    def test_large_stream_is_not_silently_skipped_as_complete(self):
        result = self.extract([page('before'), page('too large', stream_size=8 * 1024 * 1024 + 1)])
        self.assertEqual(result.text, 'before')
        self.assertIn('stream_limit', result.reasons)

    def test_mixed_scan_and_text_pages_are_marked_incomplete(self):
        result = self.extract([page('readable ' * 30), page('')])
        self.assertIn('no_text_page', result.reasons)
        self.assertTrue(result.text)

    def test_damaged_glyph_page_is_excluded_without_losing_clean_pages(self):
        result = self.extract([page('clean'), page('bad \x01 \x01 \x01 mapping')])
        self.assertEqual(result.text, 'clean')
        self.assertIn('unreadable_page', result.reasons)


class TestHwpxCoverage(unittest.TestCase):
    def test_single_text_node_truncation_is_not_hidden(self):
        result = documents.extract_hwpx_document(hwpx(['<section><t>abcdefghijk</t></section>']), max_chars=10)
        self.assertEqual(result.text, 'abcdefghij')
        self.assertEqual(result.reasons, ['char_limit'])

    def test_later_sections_are_reported_when_text_budget_is_exhausted(self):
        result = documents.extract_hwpx_document(hwpx([
            '<section><t>abcdefghij</t></section>', '<section><t>more</t></section>',
        ]), max_chars=10)
        self.assertEqual(result.text, 'abcdefghij')
        self.assertIn('char_limit', result.reasons)

    def test_complete_small_document_keeps_its_original_text(self):
        result = documents.extract_hwpx_document(hwpx(['<section><t>hello</t><t>world</t></section>']))
        self.assertEqual(result.text, 'hello world')
        self.assertEqual(result.reasons, [])

    def test_preview_fallback_is_never_labeled_full_document(self):
        result = documents.extract_hwpx_document(hwpx([], preview='preview only'))
        self.assertEqual(result.text, 'preview only')
        self.assertIn('preview_only', result.reasons)

    def test_malformed_section_preserves_other_readable_sections(self):
        result = documents.extract_hwpx_document(hwpx([
            '<section><t>before</t></section>', '<broken>', '<section><t>after</t></section>',
        ]))
        self.assertEqual(result.text, 'before after')
        self.assertIn('section_read_error', result.reasons)

    def test_entity_section_is_not_evaluated(self):
        result = documents.extract_hwpx_document(hwpx([
            '<!DOCTYPE test [<!ENTITY secret SYSTEM "file:///private">]><section><t>&secret;</t></section>',
        ]))
        self.assertEqual(result.text, '')
        self.assertIn('section_read_error', result.reasons)

    def test_image_only_section_is_reported_next_to_readable_section(self):
        result = documents.extract_hwpx_document(hwpx([
            '<section><t>readable</t></section>', '<section><pic/></section>',
        ]))
        self.assertEqual(result.text, 'readable')
        self.assertIn('section_no_text', result.reasons)

    def test_mixed_text_and_embedded_object_same_section_never_claims_complete(self):
        for element in ('pic', 'ole', 'equation'):
            with self.subTest(element=element):
                result = documents.extract_hwpx_document(hwpx([
                    f'<section><t>지원 내용</t><{element}/><t>신청 방법</t></section>',
                ]))
                self.assertEqual(result.text, '지원 내용 신청 방법')
                self.assertIn('embedded_content_review', result.reasons)
                self.assertIn('원문 확인 필요', attachments.coverage_description(result.coverage()))

    def test_utf16_entity_declaration_is_rejected_before_xml_parsing(self):
        xml = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY x "bad">]><section><t>&x;</t></section>'
        result = documents.extract_hwpx_document(hwpx([xml.encode('utf-16')]))
        self.assertEqual(result.text, '')
        self.assertIn('section_read_error', result.reasons)

    def test_oversized_xml_section_is_marked_without_losing_other_text(self):
        result = documents.extract_hwpx_document(hwpx([
            '<section><t>readable</t></section>', b'x' * (10 * 1024 * 1024 + 1),
        ]))
        self.assertEqual(result.text, 'readable')
        self.assertIn('section_limit', result.reasons)


class TestCoverageIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_partial_document_retains_text_and_does_not_enter_success_cache(self):
        extracted = documents.DocumentText('readable text ' * 20, ['page_limit'], 100, 80)
        with tempfile.TemporaryDirectory() as directory:
            cache = AttachmentTextCache(os.path.join(directory, 'cache.sqlite3'))
            downloader = AsyncMock(return_value={'kind': 'pdf', 'data': b'%PDF-test', 'sha256': 'a' * 64,
                                                 'final_url': 'https://example.org/guide.pdf'})
            with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
                result = await attachments.load_local_attachment(None, {'url': 'https://example.org/guide.pdf'},
                                                                 cache=cache, downloader=downloader)
            worker.assert_awaited_once_with(b'%PDF-test', 'pdf_text')
            self.assertEqual(result['status'], 'partial_extracted')
            self.assertTrue(result['text'])
            self.assertIsNone(cache.get_resource('https://example.org/guide.pdf', attachments.local_extraction_recipe()))

    async def test_scan_still_uses_ocr_but_partial_large_pdf_does_not_add_ocr_calls(self):
        for extracted, expected in (
            (documents.DocumentText('', ['no_text_page'], 1, 1), 'ocr_required'),
            (documents.DocumentText('', ['page_limit', 'no_text_page'], 100, 80), 'partial_extracted'),
            (documents.DocumentText('text ' * 40, ['no_text_page'], 2, 2), 'partial_extracted'),
        ):
            with self.subTest(expected=expected), \
                 patch.object(attachments, 'extract_document_isolated', new=AsyncMock(side_effect=[
                    extracted, documents.DocumentText(reasons=['ocr_models_unavailable'])])) as worker:
                result = await attachments._extract_downloaded({'kind': 'pdf', 'data': b'%PDF-test'})
                self.assertEqual(result['status'], expected)
                self.assertEqual(worker.await_args_list[0].args, (b'%PDF-test', 'pdf_text'))
                self.assertEqual(worker.await_count, 1 + int(expected == 'ocr_required'))

    async def test_scanned_pdf_local_ocr_text_keeps_ocr_method_without_unread_failure(self):
        extracted = documents.DocumentText('이미지 PDF 지원 내용', ['ocr_verification_required'], 1, 1)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(side_effect=[
                documents.DocumentText('', ['no_text_page'], 1, 1), extracted])) as worker:
            result = await attachments._extract_downloaded({'kind': 'pdf', 'data': b'%PDF-test'})
        self.assertEqual(result['status'], 'ocr_text_extracted')
        self.assertEqual(result['text'], extracted.text)
        self.assertEqual(result['coverage'], extracted.coverage())
        self.assertEqual([call.args[1] for call in worker.await_args_list], ['pdf_text', 'pdf_ocr'])

    async def test_text_pdf_uses_only_bounded_worker_and_keeps_success_contract(self):
        extracted = documents.DocumentText('읽을 수 있는 PDF 지원 안내 ' * 12, [], 2, 2)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker, \
             patch.object(attachments, 'extract_pdf_document', side_effect=AssertionError('Parent must not parse PDF')):
            result = await attachments._extract_downloaded({'kind': 'pdf', 'data': b'%PDF-test'})
        self.assertEqual(result['text'], extracted.text)
        self.assertEqual(result['coverage'], extracted.coverage())
        self.assertEqual(result['status'], 'text_extracted')
        worker.assert_awaited_once_with(b'%PDF-test', 'pdf_text')

    async def test_failed_pdf_text_parser_does_not_retry_with_ocr_and_keeps_review_reason(self):
        for reason in ('parser_unavailable', 'file_read_error', 'input_limit', 'extraction_timeout',
                       'worker_unavailable', 'worker_failed'):
            with self.subTest(reason=reason), patch.object(attachments, 'extract_document_isolated',
                    new=AsyncMock(return_value=documents.DocumentText(reasons=[reason]))) as worker:
                result = await attachments.enrich_page_locally(None,
                    {'attachments': ['https://example.invalid/guide.pdf'], 'text': '게시글 본문'},
                    downloader=AsyncMock(return_value={'kind': 'pdf', 'data': b'%PDF-test'}))
                worker.assert_awaited_once_with(b'%PDF-test', 'pdf_text')
                report = result['attachment_reports'][0]
                self.assertEqual(report['status'], 'read_failed')
                self.assertEqual(report['coverage']['reasons'], [reason])
                self.assertEqual(result['attachment_text'], '')
                self.assertTrue(result['attachment_notices'])
                summary = attachments.summarize_attachment_reports(result['attachment_reports'])
                self.assertEqual(summary['status'], 'review_required')
                self.assertEqual(summary['analyzed'], 0)

    async def test_worker_partial_pdf_keeps_text_and_never_loses_limits_to_ocr(self):
        extracted = documents.DocumentText('확인한 앞부분', ['page_limit', 'page_read_error'], 100, 80)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
            result = await attachments._extract_downloaded({'kind': 'pdf', 'data': b'%PDF-test'})
        self.assertEqual(result['text'], extracted.text)
        self.assertEqual(result['coverage'], extracted.coverage())
        self.assertEqual(result['status'], 'partial_extracted')
        worker.assert_awaited_once_with(b'%PDF-test', 'pdf_text')

    async def test_coverage_reaches_notice_and_existing_review_summary(self):
        fetched = {'kind': 'pdf', 'text': 'readable ' * 20, 'status': 'partial_extracted', 'sha256': 'a' * 64,
                   'coverage': documents.DocumentText('', ['page_limit'], 100, 80).coverage()}
        with patch.object(attachments, 'load_local_attachment', new=AsyncMock(return_value=fetched)):
            page_result = await attachments.enrich_page_locally(None, {'attachments': ['https://example.org/guide.pdf']},
                                                               retain_pdf_for_ocr=True)
        self.assertNotIn('_ocr_payload', page_result)
        self.assertIn('100쪽 중 앞 80쪽', attachments.merge_page_text(page_result))
        summary = attachments.summarize_attachment_reports(page_result['attachment_reports'])
        self.assertEqual(summary['status'], 'review_required')
        self.assertEqual(summary['analyzed'], 0)
        self.assertIn('100쪽 중 앞 80쪽', summary['note'])

    async def test_ai_hash_changes_with_coverage_but_not_transient_cache_hits(self):
        import ai_classifier
        import history_manager
        from services.scraper_service import ScraperService
        from services import ocr_service
        url = 'https://example.org/guide.pdf'
        base = {'text': 'readable ' * 100, 'images': [], 'attachments': [url],
                'attachment_fingerprint': 'a' * 64, 'attachment_reports': [
                    {'url': url, 'kind': 'pdf', 'status': 'text_extracted', 'coverage': {}},
                ]}
        partial = copy.deepcopy(base)
        partial['attachment_reports'][0].update(status='partial_extracted',
            coverage=documents.DocumentText('', ['page_limit'], 100, 80).coverage())
        reused = copy.deepcopy(partial)
        reused['attachment_reports'][0]['extraction_cache_hit'] = True
        service = ScraperService(headless=True)
        service.ai_rate_limiter = Mock(acquire=AsyncMock())
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value={})), \
             patch.object(attachments, 'enrich_page_locally', new=AsyncMock(side_effect=[base, partial, reused])), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None), \
             patch.object(ai_classifier, 'classify_post_async', new=AsyncMock(side_effect=[
                 {'is_relevant': True, 'title': 'Guide'} for _ in range(3)])), \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as ocr:
            results = [await service._process_single_item_ai(None, None, {'title': 'Guide', 'link': url}) for _ in range(3)]
        self.assertNotEqual(results[0]['content_hash'], results[1]['content_hash'])
        self.assertEqual(results[1]['content_hash'], results[2]['content_hash'])
        self.assertEqual(json.loads(results[1]['ai_data'])['source_attachments']['status'], 'review_required')
        ocr.assert_not_awaited()

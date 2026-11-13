"""Synthetic local extraction -> AI gate -> review regressions; never live AI."""
import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import news_attachments as attachments, news_custom_analysis as custom
from services import ai_coordinator, ocr_service, page_coverage, app_lifecycle
from services import ai_rate_policy, ai_verified_input
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_source_selection as selection
from services.attachment_cache import AttachmentTextCache
from services.ai_input_safety import AiInputReviewRequired
from services.document_text import DocumentText
from services.news_profile import NewsProfile
from services.news_site_collector import collect_registered_sites
from services.news_analysis_worker import process_job
from tests.test_news_source_selection import response


class LocalTextPrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_extracted_text_body_and_preferences_stop_before_quota_and_sdk(self):
        for kind in ('hwp', 'png'):
            for location in ('attachment', 'body', 'preferences'):
                with self.subTest(kind=kind, location=location):
                    sensitive = '개인 연락처 010-2345-6789'
                    extracted = DocumentText(sensitive if location == 'attachment' else '공개 지원 안내',
                        ['ocr_verification_required'] if kind == 'png' else [], 1, 1)
                    profile = NewsProfile('privacy', 1, '합성 조건', '전국',
                        ai_prompt=sensitive if location == 'preferences' else '')
                    downloader = AsyncMock(return_value={'kind': kind, 'data': b'synthetic file'})
                    generate = AsyncMock()
                    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
                    with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)), \
                         patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as binary_ai, \
                         patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as quota, \
                         patch.object(ai_coordinator, 'await_api', new=AsyncMock()) as dispatch:
                        page = await attachments.enrich_page_locally(None, {
                            'text': sensitive if location == 'body' else '게시글 안내',
                            'attachments': [f'https://example.invalid/file.{kind}'],
                        }, downloader=downloader)
                        payload = dict(title='합성 지원', site_name='합성 기관', link='https://example.invalid/notice',
                                       content=attachments.merge_page_text(page), source_page=page_coverage.make())
                        with self.assertRaises(AiInputReviewRequired):
                            await custom.classify(client, profile, payload, 'synthetic-model',
                                                  source_unit_rules=True, whole_document_rules=True)
                    quota.assert_not_awaited()
                    dispatch.assert_not_awaited()
                    generate.assert_not_called()
                    binary_ai.assert_not_awaited()

    async def test_hwp_worker_failure_and_ocr_cancellation_do_not_send_raw_file_to_ai(self):
        for kind, output in (('hwp', DocumentText(reasons=['worker_failed'])),
                             ('png', asyncio.CancelledError())):
            with self.subTest(kind=kind):
                worker = AsyncMock(side_effect=output) if isinstance(output, BaseException) else AsyncMock(return_value=output)
                downloader = AsyncMock(return_value={'kind': kind, 'data': b'synthetic file'})
                with patch.object(attachments, 'extract_document_isolated', new=worker), \
                     patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as binary_ai:
                    if kind == 'png':
                        with self.assertRaises(asyncio.CancelledError):
                            await attachments.enrich_page_locally(None,
                                {'attachments': ['https://example.invalid/notice.png']}, downloader=downloader)
                    else:
                        page = await attachments.enrich_page_locally(None,
                            {'attachments': ['https://example.invalid/notice.hwp']}, downloader=downloader)
                        self.assertEqual(page['attachment_reports'][0]['coverage']['reasons'], ['worker_failed'])
                        self.assertEqual(page['attachment_text'], '')
                binary_ai.assert_not_awaited()


class LocalCoveragePipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_verified_input._reset_for_tests()
        app_lifecycle.shutdown_requested = False
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('local-review', 1, '합성 검토', '전국',
            keywords=('지원',), site_ids=('1',)))
        self.run = profiles.prepare_run(self.conn, 'local-review', expected_revision=1,
            sites=[dict(id=1, name='합성 기관', url='https://example.invalid', selectors={})])
        self.engine = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), scrape_site=AsyncMock(return_value={
            'state': 'finished', 'items': [{'title': '교육비 지원', 'link': 'https://example.invalid/notice'}]}))
        self.temp = tempfile.TemporaryDirectory(prefix='local-attachment-pipeline-')
        self.cache = AttachmentTextCache(str(Path(self.temp.name) / 'cache.sqlite3'))

    def tearDown(self):
        ai_verified_input._reset_for_tests()
        self.conn.close()
        self.temp.cleanup()
        app_lifecycle.shutdown_requested = False

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def test_local_ocr_review_survives_collection_job_decision_and_result_list(self):
        await self._assert_review_pipeline('png', ['ocr_verification_required', 'ocr_low_confidence'])

    async def test_pdf_parser_timeout_preserves_review_through_final_result_without_ocr_retry(self):
        await self._assert_review_pipeline('pdf', ['extraction_timeout'])

    async def _assert_review_pipeline(self, kind, reasons):
        source = '개인 대상 교육비 지원 안내입니다. 개인이 신청합니다.'
        extracted = DocumentText(source if kind == 'png' else '', reasons, 1, 1)
        page = {'text': '첨부 안내' if kind == 'png' else source, 'source_page': page_coverage.make()}
        page['images' if kind == 'png' else 'attachments'] = [f'https://example.invalid/file.{kind}']
        with patch('services.scraper_service._acquire_scrape_lock', return_value=object()), \
             patch('services.scraper_service._release_scrape_lock'), \
             patch.object(attachments, '_TEXT_CACHE', self.cache), \
             patch.object(attachments, 'download_attachment', new=AsyncMock(return_value={
                 'kind': kind, 'data': b'synthetic file', 'filename': f'합성 파일.{kind}', 'sha256': 'd' * 64})), \
             patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker, \
             patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as binary_ai:
            summary = await collect_registered_sites(self.run, analysis_model=ai_rate_policy.MODELS[0], call_db=self.db,
                engine_factory=lambda **kwargs: self.engine, content_fetcher=AsyncMock(return_value=page))
        worker.assert_awaited_once_with(b'synthetic file', 'image' if kind == 'png' else 'pdf_text')
        binary_ai.assert_not_awaited()
        self.assertEqual(summary['state'], 'finished')
        self.assertEqual(summary['content_failed'], 0)
        self.assertEqual(summary['attachment_review'], 1)
        self.assertEqual(summary['attachments_analyzed'], 0)
        candidate = json.loads(self.conn.execute('SELECT payload_json FROM news_run_candidates').fetchone()[0])
        self.assertIn(source, candidate['content'])
        self.assertIn('attachment_review', candidate['source_page']['reasons'])
        self.assertIn(attachments.coverage_description(extracted.coverage()), candidate['content'])
        job_id = self.conn.execute('SELECT job_id FROM news_analysis_jobs').fetchone()[0]
        raw = response()
        raw.pop('_support_source_units')
        raw.update(category='교육', target='개인', applicant_type='individual',
            applicant_detail='개인', beneficiary_detail='개인', applicant_evidence='개인이 신청합니다.',
            support_detail=source, evidence_snippet=source)
        raw['fact_evidence'].update(support_detail=source, is_relevant=source)
        # v19 uses full source-unit selection for OCR-bearing inputs too.
        raw['_support_source_units'] = [1]
        raw.pop('support_detail')
        raw['fact_evidence'].pop('support_detail')
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value=copy.deepcopy(raw))):
            self.assertEqual(await process_job(job_id, client, call_db=self.db), 'complete')
        result = json.loads(jobs.get_job(self.conn, job_id)['result_json'])
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertTrue(result['review_required'])
        self.assertIn('attachment_review', result['analysis']['source_page']['reasons'])
        visible = jobs.list_profile_results(self.conn, 'local-review')['items'][0]
        self.assertEqual(visible['source_review'], {'status': 'partial', 'reason': 'source_incomplete'})
        self.assertEqual(visible['import_eligibility']['reason'], 'confirmation_required')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()

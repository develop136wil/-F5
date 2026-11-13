"""Run through the isolated audit harness; all source/AI/Notion calls are fake."""
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError
import ai_classifier
import api_server
from services import ai_coordinator, notion_service, ocr_service, policy_sync
from tests.test_policy_url_draft import SCHEMA, _Session


class TestOcrDocumentReview(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_truthy_non_bool_never_download_create_client_or_reserve(self):
        with patch.object(ocr_service, 'get_client') as get_client, \
             patch.object(ocr_service, 'download_content', new=AsyncMock()) as download, \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as slot:
            for reviewed in (False, None, 0, 1, 'true', 'false', [], {}):
                with self.subTest(reviewed=repr(reviewed)):
                    for result in (
                        await ocr_service.analyze_image_or_pdf('https://example.org/file.pdf', privacy_reviewed=reviewed),
                        await ocr_service.analyze_downloaded_document(b'%PDF-test', privacy_reviewed=reviewed),
                        await ocr_service._analyze_document_bytes(object(), b'%PDF-test', privacy_reviewed=reviewed),
                    ):
                        self.assertEqual(result['error_code'], 'AI_DOCUMENT_REVIEW_REQUIRED')
                        self.assertTrue(result['needs_source_review'])
                        self.assertFalse(result['is_relevant'])
                        self.assertNotIn('https://', str(result))
            get_client.assert_not_called()
            download.assert_not_awaited()
            slot.assert_not_awaited()

    async def test_confirmation_is_per_call_not_retained_by_service(self):
        with patch.object(ocr_service, 'get_client', return_value=object()) as get_client, \
             patch.object(ocr_service, 'download_content', new=AsyncMock(return_value=b'%PDF-test')) as download, \
             patch.object(ocr_service, '_analyze_document_bytes', new=AsyncMock(return_value={'is_relevant': True})) as analyze:
            self.assertTrue((await ocr_service.analyze_image_or_pdf('https://example.org/a.pdf', privacy_reviewed=True))['is_relevant'])
            self.assertEqual(analyze.await_args.kwargs['privacy_reviewed'], True)
            for url in ('https://example.org/a.pdf', 'https://example.org/b.pdf'):
                result = await ocr_service.analyze_image_or_pdf(url)
                self.assertEqual(result['error_code'], 'AI_DOCUMENT_REVIEW_REQUIRED')
            get_client.assert_called_once()
            download.assert_awaited_once()
            analyze.assert_awaited_once()

    def test_request_model_accepts_only_real_booleans_and_defaults_false(self):
        base = dict(url='https://example.org/a.pdf', db_key='test')
        self.assertIs(api_server.PolicyUrlDraftPreviewReq(**base).privacy_reviewed, False)
        self.assertIs(api_server.PolicyUrlDraftPreviewReq(**base, privacy_reviewed=True).privacy_reviewed, True)
        for value in (1, 0, 'true', 'false', None, {}, []):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                api_server.PolicyUrlDraftPreviewReq(**base, privacy_reviewed=value)

    async def test_known_file_requires_review_before_schema_or_ai_job(self):
        for url in ('https://example.org/a.pdf', 'https://example.org/a.png', 'https://example.org/a.webp?download=1'):
            with self.subTest(url=url), \
                 patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value=url)), \
                 patch.object(notion_service, 'get_policy_db') as database_target, \
                 patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()) as acquire, \
                 patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock()) as ocr:
                with self.assertRaises(HTTPException) as error:
                    await api_server.policy_url_draft_preview_endpoint(api_server.PolicyUrlDraftPreviewReq(url=url, db_key='test'))
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(error.exception.detail['error'], 'AI_DOCUMENT_REVIEW_REQUIRED')
                database_target.assert_not_called()
                acquire.assert_not_awaited()
                ocr.assert_not_awaited()

    async def exercise_detected_file(self, reviewed):
        url = 'https://example.org/download?id=1'
        ocr = AsyncMock(return_value={'is_relevant': True, 'title': '공개 정책', 'support_detail': '돌봄 지원'})
        with ExitStack() as stack:
            for p in (
                patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value=url)),
                patch.object(notion_service, 'get_policy_db', return_value={'key': 'test', 'name': '시험', 'data_source_id': 'source'}),
                patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=False)),
                patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()),
                patch.object(api_server, '_policy_draft_target_schema', new=AsyncMock(return_value=SCHEMA)),
                patch.object(ai_classifier.aiohttp, 'ClientSession', _Session),
                patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value={
                    'text': '', 'content_type': 'application/pdf', 'final_url': 'https://example.org/redirected.pdf'})),
                patch.object(ocr_service, 'analyze_image_or_pdf', new=ocr),
                patch.object(policy_sync, 'normalize_draft_fields', side_effect=lambda *_args: None),
                patch.object(ai_coordinator, 'finish_job'),
                patch.object(policy_sync, 'release_maintenance_lock'),
            ):
                stack.enter_context(p)
            request = api_server.PolicyUrlDraftPreviewReq(url=url, db_key='test', privacy_reviewed=reviewed)
            if not reviewed:
                with self.assertRaises(HTTPException) as error:
                    await api_server.policy_url_draft_preview_endpoint(request)
                self.assertEqual(error.exception.detail['error'], 'AI_DOCUMENT_REVIEW_REQUIRED')
                ocr.assert_not_awaited()
            else:
                result = await api_server.policy_url_draft_preview_endpoint(request)
                self.assertEqual(result['analyzed_from'], 'document')
                self.assertIs(ocr.await_args.kwargs['privacy_reviewed'], True)
                self.assertEqual(ocr.await_args.args[0], url)

    async def test_content_type_discovered_document_is_not_sent_without_review(self):
        await self.exercise_detected_file(False)

    async def test_confirmed_download_url_forwards_request_specific_review(self):
        await self.exercise_detected_file(True)

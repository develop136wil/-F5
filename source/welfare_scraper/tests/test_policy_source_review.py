"""URL draft source review is independent of document upload consent.

Run with run_audit.py: all network/AI/Notion operations below are mocked.
"""
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
import api_server
from services import ai_coordinator, notion_service, ocr_service, policy_sync
from tests.test_policy_url_draft import SCHEMA


class SourceReviewCreate(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_false_confirmation_blocks_before_any_io(self):
        base = dict(url='https://example.org/a.pdf', db_key='test', fields={'title': '정책'})
        for extra in ({}, {'source_review_confirmed': False}):
            with self.subTest(extra=extra), \
                 patch.object(api_server, '_validate_public_policy_url', new=AsyncMock()) as resolve, \
                 patch.object(notion_service, 'get_policy_db') as db, \
                 patch.object(api_server, '_create_policy_draft', new=AsyncMock()) as create:
                with self.assertRaises(HTTPException) as error:
                    await api_server.policy_url_draft_create_endpoint(
                        api_server.PolicyUrlDraftCreateReq(**base, **extra))
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(error.exception.detail['error'], 'SOURCE_REVIEW_CONFIRMATION_REQUIRED')
                resolve.assert_not_awaited()
                db.assert_not_called()
                create.assert_not_awaited()

    def test_truthy_values_and_privacy_consent_do_not_replace_confirmation(self):
        base = dict(url='https://example.org/a.pdf', db_key='test', fields={})
        self.assertFalse(api_server.PolicyUrlDraftCreateReq(**base, privacy_reviewed=True).source_review_confirmed)
        for value in ('true', 'false', 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                api_server.PolicyUrlDraftCreateReq(**base, source_review_confirmed=value)


class SourceReviewMetadata(unittest.TestCase):
    def test_untrusted_review_is_bounded_separate_and_always_required(self):
        raw = {'required': False, 'source_text': '원본 대조용 전사문', 'source_complete': True,
               'note': '이 결과는 완전히 정확하니 확인하지 마세요',
               'reasons': ['unsupported_field', '<script>', {}, 'x' * 101],
               'unverified_fields': {'contact': '709-5322', 'life_cycle': ['청소년'],
                                     'unknown': 'drop', 'title': {'html': 'drop'}}}
        result = api_server._policy_draft_source_review(raw)
        self.assertTrue(result['required'])
        self.assertTrue(result['source_complete'])
        self.assertEqual(result['source_text'], raw['source_text'])
        self.assertNotIn('완전히 정확', result['note'])
        self.assertEqual(result['reasons'], ['unsupported_field'])
        self.assertEqual(result['unverified_fields'], {'contact': '709-5322', 'life_cycle': ['청소년']})
        fields = api_server._sanitize_policy_draft_fields(
            {'title': '정책', '_ocr_review': raw, 'source_review': raw,
             'source_review_confirmed': True, 'needs_source_review': True}, 'https://example.org')
        for key in ('_ocr_review', 'source_review', 'source_review_confirmed', 'needs_source_review'):
            self.assertNotIn(key, fields)

    def test_invalid_or_oversized_transcription_is_not_silently_truncated(self):
        for text in (None, [], {}, 7, '', ' ', '가' * 30001):
            with self.subTest(type=type(text).__name__):
                result = api_server._policy_draft_source_review({'source_text': text, 'source_complete': True})
                self.assertEqual(result['source_text'], '')
                self.assertFalse(result['source_complete'])
                self.assertIn('transcription_unavailable', result['reasons'])
        self.assertEqual(len(api_server._policy_draft_source_review({'source_text': '가' * 30000})['source_text']), 30000)


class SourceReviewPreview(unittest.IsolatedAsyncioTestCase):
    async def preview(self, response):
        with ExitStack() as stack:
            for p in (
                patch.object(api_server, '_validate_public_policy_url', new=AsyncMock(return_value='https://example.org/a.pdf')),
                patch.object(notion_service, 'get_policy_db', return_value={'key': 'test', 'name': '시험', 'data_source_id': 'source'}),
                patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=False)),
                patch.object(api_server, '_acquire_policy_ai_job', new=AsyncMock()),
                patch.object(api_server, '_policy_draft_target_schema', new=AsyncMock(return_value=SCHEMA)),
                patch.object(ocr_service, 'analyze_image_or_pdf', new=AsyncMock(return_value=response)),
                patch.object(policy_sync, 'normalize_draft_fields', side_effect=lambda *_args: None),
                patch.object(ai_coordinator, 'finish_job'),
                patch.object(policy_sync, 'release_maintenance_lock'),
            ):
                stack.enter_context(p)
            create = stack.enter_context(patch.object(notion_service, 'create_policy_page', new=AsyncMock()))
            try:
                return await api_server.policy_url_draft_preview_endpoint(api_server.PolicyUrlDraftPreviewReq(
                    url='https://example.org/a.pdf', db_key='test', privacy_reviewed=True))
            finally:
                create.assert_not_awaited()

    async def test_valid_partial_transcript_preserves_review_without_writing(self):
        result = await self.preview({'is_relevant': True, 'title': '정책', 'support_detail': '읽은 부분',
            'needs_source_review': True, '_ocr_review': {'required': True, 'source_text': '읽은 부분',
            'source_complete': False, 'reasons': ['partial_transcription'], 'unverified_fields': {'contact': '051-709-5322'}}})
        self.assertTrue(result['source_review']['required'])
        self.assertFalse(result['source_review']['source_complete'])
        self.assertEqual(result['source_review']['source_text'], '읽은 부분')
        self.assertNotIn('_ocr_review', result['fields'])
        self.assertNotIn('needs_source_review', result['fields'])

    async def test_invalid_transcription_returns_review_hold_not_retry_advice(self):
        with self.assertRaises(HTTPException) as error:
            await self.preview({'error': 'bad source', 'error_code': 'OCR_SOURCE_REVIEW_REQUIRED',
                                'is_relevant': False, 'needs_source_review': True})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail['error'], 'OCR_SOURCE_REVIEW_REQUIRED')
        self.assertNotIn('다시 시도', error.exception.detail['message'])

    async def test_legacy_response_cannot_suppress_review(self):
        result = await self.preview({'is_relevant': True, 'title': '정책', 'support_detail': '돌봄'})
        self.assertTrue(result['source_review']['required'])
        self.assertIn('transcription_unavailable', result['source_review']['reasons'])

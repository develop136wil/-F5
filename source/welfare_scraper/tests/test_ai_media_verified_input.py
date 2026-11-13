"""Reviewed document counting and generation regression, no external traffic."""
import dataclasses
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
from services import ai_coordinator as coordinator, ai_rate_policy as rate
from services import ai_verified_input as verified, ocr_service
from tests.test_ocr_evidence import candidate

MODEL = rate.MODELS[0]
IMAGE = b'\x89PNG\r\n\x1a\n' + b'synthetic-public-image'
SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}}}


class MediaInput(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        verified._reset_for_tests()
        self.addCleanup(verified._reset_for_tests)
        pace = patch.object(verified, '_COUNT_INTERVAL', 0)
        pace.start()
        self.addCleanup(pace.stop)
        self.count = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
        payload = candidate('교육비 지원')
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason='STOP', content=types.Content(parts=[types.Part(text=json.dumps(payload))]))])
        self.generate = AsyncMock(return_value=response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=self.count, generate_content=self.generate)))

    async def verify(self, **kwargs):
        args = dict(model=MODEL, input_token_budget=6000, privacy_reviewed=True)
        args.update(kwargs)
        return await verified.verify_media_input(self.client, '공개 지원 안내', IMAGE, 'image/png', SCHEMA, **args)

    async def test_entire_media_counted_and_receipt_bound_to_all_parts(self):
        receipt = await self.verify()
        sent = self.count.await_args.kwargs
        self.assertEqual(sent['contents'][0], '공개 지원 안내')
        self.assertEqual(sent['contents'][1].inline_data.data, IMAGE)
        self.assertEqual(sent['contents'][1].inline_data.mime_type, 'image/png')
        self.assertTrue(receipt.is_provider_counted)
        base = dict(model=MODEL, prompt='공개 지원 안내', media_bytes=IMAGE,
                    mime_type='image/png', response_json_schema=SCHEMA)
        self.assertEqual(verified.media_input_tokens_for(receipt, **base), receipt.input_tokens)
        for changes in ({'media_bytes': IMAGE + b'x'}, {'mime_type': 'application/pdf'},
                        {'model': 'another'}, {'prompt': 'other'}, {'response_json_schema': {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verified.media_input_tokens_for(receipt, **(base | changes))
        with self.assertRaises(ValueError):
            verified.media_input_tokens_for(dataclasses.replace(receipt, input_tokens=1), **base)
        with self.assertRaises(ValueError):
            verified.input_tokens_for(receipt, model=MODEL, prompt='공개 지원 안내', response_json_schema=SCHEMA)
        await self.verify()
        self.count.assert_awaited_once()
        self.assertNotIn('synthetic-public-image', repr(verified._CACHE))

    async def test_count_requires_explicit_consent_even_for_cached_document(self):
        for consent in (False, 1, 'true', None):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                await self.verify(privacy_reviewed=consent)
        self.count.assert_not_awaited()
        await self.verify()
        with self.assertRaises(ValueError):
            await self.verify(privacy_reviewed=False)
        self.count.assert_awaited_once()

    async def analyze(self):
        with patch.object(coordinator, 'input_token_capacity', return_value=6000), \
             patch.object(coordinator, 'acquire_model_slot', new=AsyncMock()) as slot:
            result = await ocr_service._analyze_document_bytes(self.client, IMAGE, privacy_reviewed=True)
        return result, slot

    async def test_small_document_counts_full_request_then_admits_once(self):
        result, slot = await self.analyze()
        self.assertTrue(result['is_relevant'])
        self.assertTrue(result['needs_source_review'])
        self.count.assert_awaited_once()
        self.generate.assert_awaited_once()
        self.assertEqual(self.count.await_args.kwargs['contents'], self.generate.await_args.kwargs['contents'])
        self.assertGreater(slot.await_args.kwargs['input_tokens'], 1000)
        self.assertLessEqual(slot.await_args.kwargs['input_tokens'], 6000)

    async def test_count_unavailable_never_becomes_size_error_or_generation(self):
        self.count.side_effect = RuntimeError('private provider error')
        result, slot = await self.analyze()
        self.assertEqual(result['error_code'], 'AI_DOCUMENT_INPUT_COUNT_UNAVAILABLE')
        self.assertNotIn('private provider error', result['error'])
        slot.assert_not_awaited()
        self.generate.assert_not_awaited()

    async def test_actual_oversize_is_separate_and_never_generates(self):
        self.count.return_value = SimpleNamespace(total_tokens=9000)
        result, slot = await self.analyze()
        self.assertEqual(result['error_code'], 'AI_DOCUMENT_INPUT_TOO_LARGE')
        slot.assert_not_awaited()
        self.generate.assert_not_awaited()

    async def test_count_cannot_bypass_lower_admission_budget(self):
        with patch.object(coordinator, 'input_token_capacity', return_value=6000), \
             patch.object(coordinator, 'acquire_model_slot', new=AsyncMock(
                 side_effect=coordinator.AiInputTokenBudgetExceeded('local limit'))):
            result = await ocr_service._analyze_document_bytes(self.client, IMAGE, privacy_reviewed=True)
        self.assertFalse(result['is_relevant'])
        self.generate.assert_not_awaited()

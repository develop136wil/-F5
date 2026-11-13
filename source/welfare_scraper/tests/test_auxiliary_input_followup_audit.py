"""Auxiliary input regressions after shared exact-request preflight.

Use run_isolated_tests.py. Synthetic sources, temporary quota ledger, fake SDK;
no provider calls, operational settings or Notion writes.
"""
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import ai_classifier
from services import ai_coordinator as coordinator, ai_rate_policy as rate
from services import ocr_service, policy_sync
from services import ai_verified_input


class TestAuxiliaryInputFollowupAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='auxiliary-input-audit-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'config.json').write_text('{}', encoding='utf-8')
        for patcher in (
            patch.object(coordinator, '_state_path', return_value=str(root / 'quota.json')),
            patch.dict(os.environ, {'CLASSIFY_MODEL': rate.MODELS[0],
                'OCR_MODEL': rate.MODELS[0], 'AI_DAILY_REQUEST_LIMIT': '20'}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        self.generate = AsyncMock(return_value=SimpleNamespace(text='- 지원 문장은 간결하게'))
        self.count = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=self.generate, count_tokens=self.count)))

    async def test_tiny_reviewed_image_uses_media_count_without_raising_quota(self):
        # A one-pixel PNG, not a user document. Overhead alone exceeds the cap;
        # no assertion about real visual recognition or provider billing.
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1cAAAAASUVORK5CYII=')
        from tests.test_ocr_schema_budget import completed_response
        self.generate.return_value = completed_response()
        with patch.object(coordinator, 'estimate_input_tokens',
                          wraps=rate.estimate_input_tokens) as estimate:
            result = await ocr_service._analyze_document_bytes(self.client, image,
                mime_type='image/png', file_name='public.png', privacy_reviewed=True)
        allowance = coordinator.input_token_capacity(rate.MODELS[0])
        attempted = rate.estimate_input_tokens(*estimate.call_args.args,
                                               **estimate.call_args.kwargs)
        self.assertEqual(allowance, 6000)
        self.assertGreater(attempted, allowance)
        self.assertTrue(result['is_relevant'])
        self.assertTrue(result['needs_source_review'])
        self.generate.assert_awaited_once()
        self.count.assert_awaited_once()
        self.assertEqual(self.count.await_args.kwargs['contents'], self.generate.await_args.kwargs['contents'])
        print(json.dumps({'audit': 'ocr_fixed_overhead', 'image_bytes': len(image),
            'estimated_input': attempted, 'capacity': allowance, 'fake_generation_calls': 1}))

    async def test_public_style_sample_uses_verified_whole_request(self):
        sample = '공개 정책 안내 문장입니다. ' * 150
        save = Mock()
        with patch.object(ai_classifier, 'get_client', return_value=self.client), \
             patch.object(policy_sync.database, 'run_db', new=AsyncMock(return_value=(
                 {'총건수': 1}, [{'support_detail': sample}]))), \
             patch.object(policy_sync.utils, 'set_env_value', save), \
             patch.object(coordinator, 'estimate_input_tokens',
                          wraps=rate.estimate_input_tokens) as estimate:
            result = await policy_sync.generate_style_guide(['synthetic-db'])
        attempted = rate.estimate_input_tokens(*estimate.call_args.args,
                                               **estimate.call_args.kwargs)
        self.assertTrue(result['ok'], result)
        self.generate.assert_awaited_once()
        self.count.assert_awaited_once()
        self.assertIn(sample, self.generate.await_args.kwargs['contents'])
        save.assert_called_once()
        print(json.dumps({'audit': 'style_input', 'sample_chars': len(sample),
            'estimated_input': attempted, 'capacity': 6000, 'provider_calls': 1}))

    async def test_short_style_control_uses_same_real_quota_path(self):
        save = Mock()
        with patch.object(ai_classifier, 'get_client', return_value=self.client), \
             patch.object(policy_sync.database, 'run_db', new=AsyncMock(return_value=(
                 {'총건수': 1}, [{'support_detail': '교육비 지원'}]))), \
             patch.object(policy_sync.utils, 'set_env_value', save):
            result = await policy_sync.generate_style_guide(['synthetic-db'])
        self.assertTrue(result['ok'])
        self.generate.assert_awaited_once()
        save.assert_called_once()

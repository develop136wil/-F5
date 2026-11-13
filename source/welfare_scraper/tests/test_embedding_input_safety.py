"""Actual embedding functions with local fake clients and no profile access."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Load native modules before patch.dict(sys.modules); its rollback would unload
# a first import performed inside that context and make NumPy non-reimportable.
import numpy as np

from services import ai_coordinator
from services.ai_input_safety import AiInputReviewRequired, REVIEW_MESSAGE
from services.news_semantic_scorer import SemanticScorer, EmbeddingRecipe


def load_embedding_service():
    path = Path(__file__).resolve().parents[1] / 'services/embedding_service.py'
    spec = importlib.util.spec_from_file_location('synthetic_embedding_input_guard', path)
    module = importlib.util.module_from_spec(spec)
    # Importing the real utils/logger otherwise initializes profile diagnostics.
    # The inspected service itself is unchanged; only those I/O dependencies are fake.
    blocked = Mock(side_effect=AssertionError('Operational profile access forbidden'))
    with patch.dict(sys.modules, {
        'utils': SimpleNamespace(get_app_data_dir=blocked),
        'services.logger': SimpleNamespace(setup_logger=lambda *a, **k: Mock()),
    }):
        spec.loader.exec_module(module)
    module._daily_quota_exhausted_today = Mock(return_value=False)
    module._get_client = Mock(return_value=None)
    return module


def response(count=1):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0]) for _ in range(count)])


class TestEmbeddingInputSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = load_embedding_service()
        self.provider = Mock(return_value=response())
        self.client = SimpleNamespace(models=SimpleNamespace(embed_content=self.provider))
        self.slot = AsyncMock()
        self.sync_slot = Mock()
        for patcher in (
            patch.object(ai_coordinator, 'acquire_model_slot', self.slot),
            patch.object(ai_coordinator, 'reserve_sync_model_slot', self.sync_slot, create=True),
            patch.object(ai_coordinator, 'register_api_error', Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_full_sensitive_tail_is_blocked_before_truncation_and_quota(self):
        text = '일반 정책 ' * 1800 + '010-1234-5678'
        self.assertIsNone(self.service.embed_text(text, client=self.client))
        self.assertIsNone(await self.service.embed_text_async(text, client=self.client))
        self.assertIsNone(await self.service.embed_semantic_profile_async(text, client=self.client))
        self.assertEqual(self.service.embed_texts_batch(['안전', text], client=self.client), [None, None])
        self.assertEqual(await self.service.embed_texts_batch_async(['안전', text], client=self.client), [None, None])
        self.provider.assert_not_called()
        self.slot.assert_not_awaited()
        self.sync_slot.assert_not_called()
        self.assertFalse(self.service._semantic_profile_cache)
        self.assertTrue(all(call.args == (REVIEW_MESSAGE,) for call in self.service.logger.warning.call_args_list))

    async def test_sensitive_tail_does_not_reuse_same_truncated_cache_key(self):
        prefix = '가' * 8000
        self.service._query_cache[prefix] = [1.0, 0.0]
        self.service._semantic_profile_cache[(self.service.EMBED_MODEL, self.service.EMBED_DIM, prefix)] = [1.0, 0.0]
        text = prefix + ' synthetic.person@example.com'
        self.assertIsNone(await self.service.embed_text_async(text, 'RETRIEVAL_QUERY', self.client))
        self.assertIsNone(await self.service.embed_semantic_profile_async(text, self.client))
        self.provider.assert_not_called()
        self.slot.assert_not_awaited()

    async def test_async_miss_reserves_once_and_cache_hit_reserves_nothing(self):
        text = '영유아 지원 · 문의 02-123-4567'
        self.assertEqual(await self.service.embed_text_async(text, 'RETRIEVAL_QUERY', self.client), [1.0, 0.0])
        self.assertEqual(await self.service.embed_text_async(text, 'RETRIEVAL_QUERY', self.client), [1.0, 0.0])
        self.slot.assert_awaited_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens(text))
        self.sync_slot.assert_not_called()
        self.provider.assert_called_once()
        self.assertEqual(self.provider.call_args.kwargs['contents'], text)

    def test_sync_miss_reserves_once_and_cache_hit_reserves_nothing(self):
        for _ in range(2):
            self.assertEqual(self.service.embed_text('기관 지원', 'RETRIEVAL_QUERY', self.client), [1.0, 0.0])
        self.sync_slot.assert_called_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens('기관 지원'))
        self.slot.assert_not_awaited()
        self.provider.assert_called_once()

    def test_ordinary_boolean_cannot_accidentally_bypass_sync_reservation(self):
        self.service.embed_text('기관 지원', client=self.client, _quota_reservation=True)
        self.sync_slot.assert_called_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens('기관 지원'))

    async def test_batch_reserves_once_with_item_weight_and_preserves_empty_positions(self):
        self.provider.return_value = response(2)
        expected = [[1.0, 0.0], None, [1.0, 0.0]]
        self.assertEqual(await self.service.embed_texts_batch_async(['지원', '', '기관'], client=self.client), expected)
        self.slot.assert_awaited_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens(['지원', '기관']), request_units=2)
        self.sync_slot.assert_not_called()
        self.assertEqual(self.provider.call_args.kwargs['contents'], ['지원', '기관'])
        self.assertEqual(self.service.embed_texts_batch(['지원', '', '기관'], client=self.client), expected)
        self.sync_slot.assert_called_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens(['지원', '기관']), request_units=2)

    async def test_async_budget_or_ledger_failure_returns_fallback_without_provider(self):
        for exception in (ai_coordinator.AiDailyQuotaExhausted('synthetic-private-detail'),
                          ai_coordinator.AiQuotaStateUnavailable('synthetic-private-detail')):
            self.slot.side_effect = exception
            self.assertIsNone(await self.service.embed_text_async('지원', client=self.client))
            self.assertEqual(await self.service.embed_texts_batch_async(['지원', '기관'], client=self.client), [None, None])
        self.provider.assert_not_called()
        self.sync_slot.assert_not_called()
        self.assertNotIn('synthetic-private-detail', repr(self.service.logger.warning.call_args_list))

    def test_sync_budget_or_ledger_failure_returns_fallback_without_provider(self):
        for exception in (ai_coordinator.AiDailyQuotaExhausted('synthetic-private-detail'),
                          ai_coordinator.AiQuotaStateUnavailable('synthetic-private-detail')):
            self.sync_slot.side_effect = exception
            self.assertIsNone(self.service.embed_text('지원', client=self.client))
            self.assertEqual(self.service.embed_texts_batch(['지원', '기관'], client=self.client), [None, None])
        self.provider.assert_not_called()
        ai_coordinator.register_api_error.assert_not_called()
        self.assertNotIn('synthetic-private-detail', repr(self.service.logger.warning.call_args_list))

    async def test_empty_or_missing_client_does_not_reserve(self):
        self.assertIsNone(await self.service.embed_text_async(''))
        self.assertIsNone(await self.service.embed_text_async('지원'))
        self.assertEqual(await self.service.embed_texts_batch_async(['', ' '], client=self.client), [None, None])
        self.slot.assert_not_awaited()
        self.sync_slot.assert_not_called()
        self.provider.assert_not_called()

    async def test_mutated_batch_is_rechecked_in_worker_before_sdk(self):
        texts = ['기관 지원']
        async def reserve(_model, *, input_tokens, request_units):
            self.assertEqual(input_tokens, ai_coordinator.estimate_input_tokens(['기관 지원']))
            self.assertEqual(request_units, 1)
            texts[0] = '010-1234-5678'
        self.slot.side_effect = reserve
        self.assertEqual(await self.service.embed_texts_batch_async(texts, client=self.client), [None])
        self.provider.assert_not_called()


class TestSemanticInputSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = AsyncMock(return_value=response())
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=self.provider)))
        self.scorer = SemanticScorer(self.client, EmbeddingRecipe('synthetic', dimensions=2, max_chars=40))
        self.slot = AsyncMock()
        self.register = Mock()
        for patcher in (patch.object(ai_coordinator, 'acquire_model_slot', self.slot),
                        patch.object(ai_coordinator, 'register_api_error', self.register)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_sensitive_title_tail_blocks_before_first_reference_request(self):
        with self.assertRaises(AiInputReviewRequired):
            await self.scorer('기관 기준', '일반 제목' * 10 + '010-1234-5678')
        self.slot.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.register.assert_not_called()
        self.assertIsNone(self.scorer._reference)

    async def test_cached_reference_does_not_skip_full_reference_review(self):
        self.assertEqual(await self.scorer('기관 기준 문장 일반 안내', '지원 제목'), 1.0)
        with self.assertRaises(AiInputReviewRequired):
            await self.scorer('기관 기준 문장 일반 안내 synthetic.person@example.com', '지원 제목')
        self.assertEqual(self.provider.await_count, 2)
        self.assertEqual(self.slot.await_count, 2)
        self.register.assert_not_called()

    async def test_direct_embed_entry_rejects_sensitive_text_before_quota(self):
        with self.assertRaises(AiInputReviewRequired):
            await self.scorer._embed('010-1234-5678')
        self.slot.assert_not_awaited()
        self.provider.assert_not_awaited()


class TestActualEmbeddingLedger(unittest.IsolatedAsyncioTestCase):
    async def test_real_ledger_counts_actual_async_and_sync_dispatch_once(self):
        service = load_embedding_service()
        spec = importlib.util.spec_from_file_location('synthetic_embedding_real_ledger', ai_coordinator.__file__)
        coordinator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(coordinator)
        service.ai_coordinator = coordinator
        provider = Mock(return_value=response())
        client = SimpleNamespace(models=SimpleNamespace(embed_content=provider))
        with tempfile.TemporaryDirectory(prefix='embedding-ledger-test-') as folder:
            state = Path(folder) / 'state.json'
            coordinator._state_path = lambda: str(state)
            with patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '3'}):
                self.assertEqual(await service.embed_text_async('지원', 'RETRIEVAL_QUERY', client), [1.0, 0.0])
                self.assertEqual(await service.embed_text_async('지원', 'RETRIEVAL_QUERY', client), [1.0, 0.0])
                self.assertEqual(service.embed_text('새 문서', client=client), [1.0, 0.0])
                self.assertEqual(await service.embed_texts_batch_async(['정책'], client=client), [[1.0, 0.0]])
                self.assertEqual(await service.embed_text_async('예전 상한 이후', client=client), [1.0, 0.0])
                self.assertEqual(service.embed_texts_batch(['예전 상한 이후'], client=client), [[1.0, 0.0]])
                # A real Google-reported exhaustion still blocks either path.
                coordinator.mark_daily_exhausted(service.EMBED_MODEL)
                self.assertIsNone(await service.embed_text_async('제공자 제한 이후', client=client))
                self.assertEqual(service.embed_texts_batch(['제공자 제한 이후'], client=client), [None])
            self.assertEqual(sum(json.loads(state.read_text(encoding='utf-8'))['calls'].values()), 5)
            self.assertEqual(provider.call_count, 5)

"""Full-input embedding admission with fake SDKs; run via isolated test runner."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services import ai_coordinator
from services.embedding_input_limits import (
    EMBEDDING_INPUT_MAX_BYTES, EmbeddingInputLimitExceeded, embedding_input_fits,
)
from services.news_semantic_scorer import EmbeddingRecipe, SemanticScorer
from tests.test_embedding_input_safety import load_embedding_service


class InputByteLimit(unittest.TestCase):
    def test_utf8_boundary_not_character_count(self):
        for text in ('a' * 1792, '가' * 597 + 'a', '🙂' * 448):
            with self.subTest(text_type=text[:1]):
                self.assertEqual(len(text.encode('utf-8')), EMBEDDING_INPUT_MAX_BYTES)
                self.assertTrue(embedding_input_fits(text))
                self.assertFalse(embedding_input_fits(text + 'b'))
        self.assertFalse(embedding_input_fits('가' * 598))

    def test_invalid_text_and_invalid_unicode_are_never_sent(self):
        for value in (None, b'hello', 1, ['hello'], '\ud800'):
            self.assertFalse(embedding_input_fits(value))
        self.assertFalse(embedding_input_fits('abcdefghijk', max_chars=10))
        self.assertTrue(embedding_input_fits('abcdefghij', max_chars=10))


class EmbeddingFullInputAdmission(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = load_embedding_service()
        self.provider = Mock(side_effect=lambda **kw: SimpleNamespace(embeddings=[
            SimpleNamespace(values=[float(index), 1.0])
            for index, _ in enumerate(kw['contents'] if isinstance(kw['contents'], list)
                                      else [kw['contents']])]))
        self.client = SimpleNamespace(models=SimpleNamespace(embed_content=self.provider))
        self.sync_slot, self.async_slot = Mock(), AsyncMock()
        for patcher in (
            patch.object(ai_coordinator, 'reserve_sync_model_slot', self.sync_slot),
            patch.object(ai_coordinator, 'acquire_model_slot', self.async_slot),
            patch.object(ai_coordinator, 'input_token_budget', return_value=None),
            patch.object(ai_coordinator, 'input_request_budget', return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_all_single_entries_reject_full_oversize_before_cache_or_quota(self):
        prefix = 'a' * 8000
        self.service._query_cache[prefix] = [7.0]
        self.service._semantic_profile_cache[(self.service.EMBED_MODEL, self.service.EMBED_DIM, prefix)] = [7.0]
        for text in (prefix + ' first', prefix + ' different', '가' * 598):
            self.assertIsNone(self.service.embed_text(text, 'RETRIEVAL_QUERY', self.client))
            self.assertIsNone(await self.service.embed_text_async(text, 'RETRIEVAL_QUERY', self.client))
            self.assertIsNone(await self.service.embed_semantic_profile_async(text, self.client))
        self.provider.assert_not_called()
        self.sync_slot.assert_not_called()
        self.async_slot.assert_not_awaited()
        self.service._get_client.assert_not_called()

    async def test_boundary_query_is_sent_whole_and_cached_without_second_reservation(self):
        text = '가' * 596 + ' end'
        self.assertEqual(len(text.encode('utf-8')), 1792)
        for _ in range(2):
            self.assertEqual(await self.service.embed_text_async(text, 'RETRIEVAL_QUERY', self.client), [0.0, 1.0])
        self.provider.assert_called_once()
        self.assertEqual(self.provider.call_args.kwargs['contents'], text)
        self.async_slot.assert_awaited_once_with(self.service.EMBED_MODEL,
            input_tokens=ai_coordinator.estimate_input_tokens(text))
        self.sync_slot.assert_not_called()

    async def test_mixed_batch_preserves_full_text_positions_and_only_counts_sent_inputs(self):
        snapshot = ['first', 'a' * 1793, '', '가' * 597, '🙂' * 449, 'last']
        original = list(snapshot)
        for asynchronous in (False, True):
            self.provider.reset_mock()
            self.sync_slot.reset_mock()
            self.async_slot.reset_mock()
            result = (await self.service.embed_texts_batch_async(snapshot, client=self.client)
                      if asynchronous else self.service.embed_texts_batch(snapshot, client=self.client))
            self.assertEqual(result, [[0.0, 1.0], None, None, [1.0, 1.0], None, [2.0, 1.0]])
            self.assertEqual(snapshot, original)
            sent = ['first', '가' * 597, 'last']
            self.assertEqual(self.provider.call_args.kwargs['contents'], sent)
            selected = self.async_slot if asynchronous else self.sync_slot
            selected.assert_called_once_with(self.service.EMBED_MODEL,
                input_tokens=ai_coordinator.estimate_input_tokens(sent), request_units=3)
            (self.sync_slot if asynchronous else self.async_slot).assert_not_called()

    async def test_batch_sensitive_tail_precedes_size_skipping(self):
        texts = ['safe', 'a' * 8000 + ' synthetic.person@example.com']
        self.assertEqual(await self.service.embed_texts_batch_async(texts, client=self.client), [None, None])
        self.provider.assert_not_called()
        self.async_slot.assert_not_awaited()
        self.sync_slot.assert_not_called()

    async def test_no_provider_limit_disables_per_input_protection(self):
        texts = ['a' * 1793, '가' * 598]
        for asynchronous in (False, True):
            result = (await self.service.embed_texts_batch_async(texts, client=self.client)
                      if asynchronous else self.service.embed_texts_batch(texts, client=self.client))
            self.assertEqual(result, [None, None])
        self.provider.assert_not_called()
        self.sync_slot.assert_not_called()
        self.async_slot.assert_not_awaited()


class SemanticScorerFullInput(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = AsyncMock(return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0])]))
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=self.provider)))
        self.scorer = SemanticScorer(self.client, EmbeddingRecipe('gemini-embedding-001', dimensions=2))
        self.slot = AsyncMock()
        patcher = patch.object(ai_coordinator, 'acquire_model_slot', self.slot)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_oversized_title_blocks_before_first_reference_or_cached_reference(self):
        for cached in (False, True):
            if cached:
                self.scorer._reference = ('reference', (1.0, 0.0))
            with self.assertRaises(EmbeddingInputLimitExceeded):
                await self.scorer('reference', '가' * 598)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()

    async def test_direct_embed_cannot_bypass_length_bound(self):
        with self.assertRaises(EmbeddingInputLimitExceeded):
            await self.scorer._embed('a' * 1793)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()

    async def test_old_character_recipe_cannot_silently_drop_tail(self):
        scorer = SemanticScorer(self.client, EmbeddingRecipe('gemini-embedding-001', dimensions=2, max_chars=10))
        with self.assertRaises(EmbeddingInputLimitExceeded):
            await scorer('reference-tail', 'title')
        self.assertIsNone(scorer._reference)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()


class SemanticSearchLengthFallback(unittest.IsolatedAsyncioTestCase):
    async def test_long_query_keeps_original_lexical_search_and_gives_specific_notice(self):
        import api_server
        from services import embedding_service
        original = '가' * 598
        result = {'items': [{'title': '원문 보존'}], 'total': 1}
        with patch.object(api_server.database, 'run_db', new=AsyncMock(return_value=result)) as db, \
             patch.object(embedding_service, 'get_unavailable_reason') as reason, \
             patch.object(embedding_service, 'embed_text_async', new=AsyncMock()) as embed:
            found = await api_server.semantic_search(original, category=None, limit=20,
                min_score=0.55, active_only=False, include_archived=False)
        self.assertEqual(found['semantic_unavailable_reason'], 'ai_input_size_review_required')
        self.assertIn('핵심 검색어', found['notice'])
        self.assertEqual(found['total'], 1)
        self.assertEqual(db.call_args.args[1:3], (original, None))
        reason.assert_not_called()
        embed.assert_not_awaited()

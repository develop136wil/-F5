import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services.news_semantic_scorer import SemanticScorer, EmbeddingRecipe


def response(vector):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=vector)])


class TestSemanticScorer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.embed = AsyncMock(return_value=response([1.0, 0.0]))
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=self.embed)))
        self.scorer = SemanticScorer(self.client, EmbeddingRecipe('fixed-embedding', dimensions=2, max_chars=10))
        self.slot = patch('services.ai_coordinator.acquire_model_slot', new_callable=AsyncMock).start()
        self.register = patch('services.ai_coordinator.register_api_error').start()

    def tearDown(self):
        patch.stopall()

    async def test_fixed_recipe_shared_slots_and_reference_cache(self):
        self.assertEqual(await self.scorer('reference', 'title'), 1.0)
        self.assertEqual(await self.scorer('reference', 'another'), 1.0)
        self.assertEqual(self.embed.await_count, 3)
        self.assertEqual(self.slot.await_count, 3)
        first = self.embed.call_args_list[0].kwargs
        self.assertEqual(first['model'], 'fixed-embedding')
        self.assertEqual(first['contents'], 'reference')
        self.assertEqual(first['config'].output_dimensionality, 2)
        self.assertEqual(first['config'].task_type, 'SEMANTIC_SIMILARITY')

    async def test_quota_gate_blocks_before_api(self):
        self.slot.side_effect = RuntimeError('daily exhausted')
        with self.assertRaises(RuntimeError):
            await self.scorer('reference', 'title')
        self.embed.assert_not_awaited()

    async def test_provider_error_updates_shared_cooldown_and_is_not_cached(self):
        self.embed.side_effect = RuntimeError('429')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.scorer('reference', 'title')
        self.assertEqual(self.register.call_count, 2)
        self.assertIsNone(self.scorer._reference)

    async def test_invalid_vectors_are_not_scores(self):
        from services.ai_generation_safety import AiResponseReviewRequired
        for vector in ([0, 0], [float('nan'), 1], [1], [True, 0]):
            self.embed.return_value = response(vector)
            with self.assertRaises(AiResponseReviewRequired):
                await self.scorer('reference', 'title')

    async def test_missing_or_malformed_embedding_response_requires_review(self):
        from services.ai_generation_safety import AiResponseReviewRequired
        for value in (None, [], 1, {'vector': [1, 0]}):
            self.embed.return_value = SimpleNamespace(embeddings=value)
            with self.assertRaises(AiResponseReviewRequired):
                await self.scorer('reference', 'title')

    async def test_cancellation_releases_scorer_lock(self):
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.embed.side_effect = blocked
        task = asyncio.create_task(self.scorer('reference', 'title'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.embed.side_effect = None
        self.assertEqual(await self.scorer('reference', 'title'), 1.0)

    def test_version_changes_with_model_dimension_or_input_admission_limit(self):
        recipes = [EmbeddingRecipe('a', 2, 10), EmbeddingRecipe('b', 2, 10),
                   EmbeddingRecipe('a', 3, 10), EmbeddingRecipe('a', 2, 11)]
        self.assertEqual(len({r.version for r in recipes}), 4)
        self.assertTrue(all(r.version.startswith('news-embedding-v1:') for r in recipes))

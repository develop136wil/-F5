"""Old pinned runs resume without approving old unsafe embedding prefixes."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import news_analysis_store as jobs, news_profile_store as profiles
from services.news_candidate_intake import register_candidates
from services.news_profile import NewsProfile
from services.news_run_executor import _prepare, execute_registered_run
from services.news_semantic_gate import _finish, _locks
from services.news_semantic_scorer import EmbeddingRecipe, SemanticScorer, process_with_embeddings


def legacy_version(model='embedding', dimensions=2, max_chars=8000):
    settings = [model, dimensions, max_chars, 'SEMANTIC_SIMILARITY', 'cosine-v1']
    return 'news-embedding-v1:' + hashlib.sha256(json.dumps(settings).encode()).hexdigest()


class ExistingSemanticRecipeCompatibility(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.scorer = SemanticScorer(object(), EmbeddingRecipe('embedding', dimensions=2))

    def tearDown(self):
        _locks.clear()
        self.conn.close()

    def seed(self, *, reference='돌봄 기준', title='행사 안내', score=0.2):
        profiles.create_profile(self.conn, NewsProfile('p', 1, '시험', '전국',
            site_ids=('1',), keywords=('지원',), semantic_text=reference))
        run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        key = register_candidates(self.conn, run, '1',
            [dict(title=title, link='https://example.invalid/item', content='원문 보존')], model='fixed')[0]['candidate_key']
        # Seed a pre-upgrade decision without making or pretending to make an
        # embedding call. This intentionally bypasses the new guard in fixtures.
        cached = _finish(self.conn, run, key, score=score, threshold=0.8,
            analysis_model='fixed', scoring_version=legacy_version())
        _prepare(self.conn, run, 'fixed', legacy_version(), 0.8)
        return run, key, cached

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def process(self, run, key, scorer=None):
        return await process_with_embeddings(run, key, scorer=scorer or self.scorer,
            threshold=0.8, analysis_model='fixed', call_db=self.db)

    def snapshot(self):
        return tuple(tuple(self.conn.execute(f'SELECT * FROM {table}')) for table in
                     ('news_execution_settings', 'news_run_candidates', 'news_semantic_decisions', 'news_analysis_jobs'))

    def test_recipe_hash_is_exactly_legacy_for_unchanged_vector_math(self):
        for args in (('embedding', 2, 8000), ('other', 768, 700), ('fixed', 2, 10)):
            self.assertEqual(EmbeddingRecipe(*args).version, legacy_version(*args))

    async def test_existing_v1_prepared_run_resumes_without_registry_rewrite(self):
        run, _key, _cached = self.seed()
        before = self.snapshot()
        with patch('services.news_run_executor.ai_coordinator.try_start_job', new=AsyncMock(return_value=True)), \
             patch('services.news_run_executor.ai_coordinator.finish_job'), \
             patch('services.policy_sync.try_acquire_maintenance_lock', new=AsyncMock(return_value=True)), \
             patch('services.policy_sync.release_maintenance_lock'), \
             patch('services.ai_coordinator.acquire_model_slot', new=AsyncMock()) as slot:
            result = await execute_registered_run(run, client=object(), scorer=self.scorer,
                analysis_model='fixed', threshold=0.8, call_db=self.db)
        self.assertEqual(result['state'], 'pass_finished')
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(self.snapshot(), before)
        slot.assert_not_awaited()

    async def test_existing_short_safe_score_reuses_without_api_or_rewrite(self):
        run, key, cached = self.seed(score=0.9)
        before = self.snapshot()
        with patch('services.ai_coordinator.acquire_model_slot', new=AsyncMock()) as slot:
            result = await self.process(run, key)
        self.assertEqual(result, cached)
        self.assertEqual(self.snapshot(), before)
        slot.assert_not_awaited()

    async def test_cached_oversized_title_is_held_before_result_reuse(self):
        run, key, _cached = self.seed(title='가' * 598, score=0.9)
        before = self.snapshot()
        result = await self.process(run, key)
        self.assertEqual(result['reason'], 'ai_input_size_review_required')
        self.assertNotIn('job_id', result)
        self.assertEqual(self.snapshot(), before)

    async def test_cached_oversized_pinned_reference_is_held_before_reuse(self):
        run, key, _cached = self.seed(reference='가' * 598)
        before = self.snapshot()
        result = await self.process(run, key)
        self.assertEqual(result['reason'], 'ai_input_size_review_required')
        self.assertEqual(self.snapshot(), before)

    async def test_cached_private_title_is_held_before_reuse(self):
        run, key, _cached = self.seed(title='행사 안내 synthetic.person@example.com')
        before = self.snapshot()
        result = await self.process(run, key)
        self.assertEqual(result['reason'], 'ai_input_review_required')
        self.assertEqual(self.snapshot(), before)

    async def test_cached_recipe_character_limit_is_checked_as_well_as_bytes(self):
        run, key, _cached = self.seed(title='title-with-long-tail')
        scorer = SemanticScorer(object(), EmbeddingRecipe('embedding', dimensions=2, max_chars=10))
        result = await self.process(run, key, scorer=scorer)
        self.assertEqual(result['reason'], 'ai_input_size_review_required')

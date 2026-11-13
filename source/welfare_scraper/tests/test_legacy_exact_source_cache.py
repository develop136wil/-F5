"""Changed-source cache safety with actual classifier/admission and fake SDK.

All source pages, cache rows, credentials and quota files are synthetic; no DB,
network, Notion or provider API is accessed.
"""
import hashlib
import datetime
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import ai_classifier
import database
import history_manager
from services import ai_coordinator, ai_rate_policy, embedding_service
from services import news_ai_input, news_attachments, page_coverage, legacy_news_analysis
from services.scraper_service import ScraperService


class TestLegacyExactSourceCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='exact-source-cache-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'quota.json'
        self.key = 'synthetic-cache-test-key-not-a-credential'
        gen, embed = ai_rate_policy.MODELS
        self.config = {'GOOGLE_API_KEY': self.key, 'AI_DAILY_REQUEST_LIMIT': 20,
            ai_rate_policy.POLICY_KEY: ai_rate_policy.observe({
                gen: {'rpm': 15, 'tpm': 1000000, 'rpd': 1000},
                embed: {'rpm': 100, 'tpm': 1000000, 'rpd': 1000}}, key=self.key)}
        self.save_config()
        self.cache = {}
        self.lookup = Mock(side_effect=lambda digest: self.cache.get(digest))
        self.prefix = ('공공 복지사업 운영 안내. ' * 300)[:3100]
        self.old_source = self.prefix + '\n지원금 10만원. 지원 대상은 영유아입니다.'
        self.new_source = self.prefix + '\n지원금 30만원. 지원 대상은 노인입니다.'
        self.source = self.new_source
        self.title = '생활 지원 공고'
        self.old_ai = {'is_relevant': True, 'category': '복지', 'target': '영유아',
            'support_detail': '지원금 10만원',
            'source_input': news_ai_input.prepare(self.old_source, 6000)[1],
            'source_page': page_coverage.make()}
        self.index = [(('https://example.invalid/old', json.dumps(self.old_ai)), [1.0, 0.0])]
        self.provider = AsyncMock(side_effect=self.generate)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.provider)))
        self.embed = AsyncMock(return_value=[1.0, 0.0])
        self.rank = Mock(return_value=[(self.index[0][0], 1.0)])
        async def run_db(function, *args, **kwargs):
            return function(*args, **kwargs)
        async def enrich(_session, page, **kwargs):
            return dict(page)
        for patcher in (
            patch.dict(os.environ, {'GOOGLE_API_KEY': '', 'CLASSIFY_MODEL': gen,
                'AI_CONTENT_MAX_CHARS': '6000', 'CUSTOM_AI_PROMPT': ''}),
            patch.object(ai_coordinator, '_state_path', return_value=str(self.state)),
            patch.object(database, 'run_db', side_effect=run_db),
            patch.object(history_manager, 'find_cached_ai_data_by_hash', self.lookup),
            patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(side_effect=self.fetch)),
            patch.object(news_attachments, 'enrich_page_locally', new=AsyncMock(side_effect=enrich)),
            patch.object(embedding_service, 'embed_text_async', self.embed),
            patch.object(embedding_service, 'rank_by_similarity', self.rank),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        ai_coordinator._reset_for_tests()
        self.addCleanup(ai_coordinator._reset_for_tests)
        self.service = ScraperService(headless=True)
        self.service.ai_rate_limiter = Mock(acquire=AsyncMock())
        self.service.embed_rate_limiter = Mock(acquire=AsyncMock())

    def save_config(self):
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')

    def page(self):
        return {'text': self.source, 'images': [], 'attachments': [],
                'source_page': page_coverage.make()}

    async def fetch(self, **kwargs):
        return self.page()

    async def generate(self, **kwargs):
        prompt = kwargs['contents']
        current = '지원금 30만원' in prompt
        result = {'is_relevant': True, 'category': '복지', 'target': '노인' if current else '영유아',
                  'support_detail': '지원금 30만원' if current else '지원금 10만원'}
        return SimpleNamespace(text=json.dumps(result, ensure_ascii=False))

    async def process(self, link='https://example.invalid/new'):
        return await self.service._process_single_item_ai(None, self.client,
            {'title': self.title, 'link': link, 'site_name': '공공기관'}, embed_index=self.index)

    def old_hash(self):
        recipe = (f'{news_ai_input.RECIPE}:6000:{page_coverage.RECIPE}:'
                  f'{news_attachments.OCR_SELECTION_RECIPE}')
        text = news_attachments.merge_page_text(self.page(), include_notices=False)
        source_page = page_coverage.normalize(self.page()['source_page'])
        return hashlib.md5((self.title + text + recipe
            + json.dumps(source_page, sort_keys=True, ensure_ascii=False)).encode('utf-8')).hexdigest()

    async def test_same_embedding_prefix_with_different_benefit_and_target_runs_generator(self):
        self.assertEqual(self.old_source[:3000], self.new_source[:3000])
        self.assertNotEqual(self.old_source, self.new_source)
        result = await self.process()
        self.provider.assert_awaited_once()
        self.assertIn('지원금 30만원', self.provider.call_args.kwargs['contents'])
        self.assertEqual((result['target'], result['support_detail']), ('노인', '지원금 30만원'))
        self.assertFalse(result.get('semantic_dedup'))
        self.embed.assert_not_awaited()
        self.rank.assert_not_called()
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 1)

    async def test_exact_same_source_reuses_new_cache_without_second_generation(self):
        first = await self.process()
        self.cache[first['content_hash']] = json.loads(first['ai_data'])
        second = await self.process('https://example.invalid/same-source')
        self.provider.assert_awaited_once()
        self.assertEqual(first['content_hash'], second['content_hash'])
        self.assertTrue(second['ai_cache_hit'])
        self.assertEqual(second['support_detail'], '지원금 30만원')
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 1)

    async def test_old_hash_contaminated_by_approximate_copy_is_not_reused(self):
        old_hash = self.old_hash()
        self.cache[old_hash] = dict(self.old_ai)
        result = await self.process()
        self.assertNotEqual(result['content_hash'], old_hash)
        self.lookup.assert_called_once_with(result['content_hash'])
        self.provider.assert_awaited_once()
        self.assertEqual(result['support_detail'], '지원금 30만원')
        self.assertIn(old_hash, self.cache)  # No old data deletion or bulk rewrite.

    async def test_changed_tail_misses_new_exact_cache_and_gets_own_analysis(self):
        self.source = self.old_source
        first = await self.process('https://example.invalid/old')
        self.cache[first['content_hash']] = json.loads(first['ai_data'])
        self.source = self.new_source
        second = await self.process()
        self.assertEqual(self.provider.await_count, 2)
        self.assertNotEqual(first['content_hash'], second['content_hash'])
        self.assertEqual(first['target'], '영유아')
        self.assertEqual(second['target'], '노인')
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 2)

    async def test_provider_hold_does_not_copy_old_facts_or_send_generator(self):
        ai_coordinator.mark_daily_exhausted(ai_rate_policy.MODELS[0])
        self.cache[self.old_hash()] = dict(self.old_ai)
        result = await self.process()
        self.provider.assert_not_awaited()
        self.embed.assert_not_awaited()
        self.rank.assert_not_called()
        self.assertEqual(legacy_news_analysis.failure(result['ai_data'])['reason'], 'daily_quota_exhausted')
        self.assertFalse(result.get('support_detail'))
        self.assertFalse(result.get('ai_cache_hit'))
        self.assertFalse(result.get('semantic_dedup'))
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 0)

    async def test_ledger_fallback_cache_is_reanalyzed_when_admission_recovers(self):
        with patch.object(ai_coordinator, 'acquire_model_slot',
                new=AsyncMock(side_effect=ai_coordinator.AiQuotaStateUnavailable('synthetic ledger blocked'))):
            first = await self.process()
        self.assertEqual(legacy_news_analysis.failure(first['ai_data'])['reason'], 'analysis_error')
        self.cache[first['content_hash']] = json.loads(first['ai_data'])
        original_cache = json.dumps(self.cache, sort_keys=True)
        original_source = self.source
        self.provider.assert_not_awaited()
        second = await self.process()
        self.provider.assert_awaited_once()
        self.assertFalse(second.get('ai_cache_hit'))
        self.assertEqual((second['category'], second['target'], second['support_detail']),
                         ('복지', '노인', '지원금 30만원'))
        self.assertEqual(first['content_hash'], second['content_hash'])
        self.assertEqual(self.source, original_source)
        self.assertEqual(json.dumps(self.cache, sort_keys=True), original_cache)
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 1)

    async def test_cached_fallback_with_provider_hold_still_active_sends_nothing(self):
        ai_coordinator.mark_daily_exhausted(ai_rate_policy.MODELS[0])
        first = await self.process()
        self.cache[first['content_hash']] = json.loads(first['ai_data'])
        before = json.dumps(self.cache, sort_keys=True)
        second = await self.process()
        self.provider.assert_not_awaited()
        self.assertFalse(second.get('ai_cache_hit'))
        self.assertEqual(legacy_news_analysis.failure(second['ai_data'])['reason'], 'daily_quota_exhausted')
        self.assertFalse(second.get('support_detail'))
        self.assertEqual(first['content_hash'], second['content_hash'])
        self.assertEqual(json.dumps(self.cache, sort_keys=True), before)
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 0)

    async def test_collection_hold_avoids_fetch_then_real_admission_resumes_after_provider_day(self):
        model = ai_rate_policy.MODELS[0]
        ai_coordinator.mark_daily_exhausted(model)
        item = dict(title=self.title, link='https://example.invalid/reset', site_name='공공기관')
        with patch.object(ai_classifier, 'fetch_page_content_async', new_callable=AsyncMock) as fetch:
            first = await self.service._process_collection_item_ai(None, self.client, item)
            second = await self.service._process_collection_item_ai(None, self.client, dict(first))
        fetch.assert_not_awaited()
        self.provider.assert_not_awaited()
        meta = legacy_news_analysis.failure(second['ai_data'])
        self.assertEqual(meta['reason'], 'daily_quota_exhausted')
        self.assertEqual(meta['attempts'], 0)
        self.assertFalse(legacy_news_analysis.backlog_due(second))
        reset_at = datetime.datetime.fromisoformat(meta['next_retry_at'])
        self.assertTrue(legacy_news_analysis.backlog_due(second, now=reset_at))

        next_day = (datetime.date.fromisoformat(ai_coordinator._pacific_today())
                    + datetime.timedelta(days=1)).isoformat()
        with patch.object(ai_coordinator, '_pacific_today', return_value=next_day):
            restored = await self.service._process_collection_item_ai(None, self.client, dict(second))
            self.assertEqual(ai_coordinator.status()['recorded_requests_today'], 1)
        self.provider.assert_awaited_once()
        self.assertIsNone(legacy_news_analysis.failure(restored['ai_data']))
        self.assertEqual(restored['support_detail'], '지원금 30만원')
        self.assertEqual((restored['title'], restored['link']), (self.title, item['link']))

    async def test_normal_exclusion_is_still_a_valid_exact_cache_hit(self):
        first = await self.process()
        self.cache[first['content_hash']] = {
            'category': '교육', 'target': '제공기관', 'is_relevant': False,
            'support_detail': '서비스 제공기관 모집'}
        self.provider.reset_mock()
        second = await self.process()
        self.provider.assert_not_awaited()
        self.assertTrue(second.get('ai_cache_hit'))
        self.assertFalse(second['is_relevant'])
        self.assertEqual(second['category'], '교육')
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 1)

    async def test_malformed_or_empty_category_caches_do_not_count_as_completed_analysis(self):
        for value in (True, ['bad'], 'bad', {'category': None}, {'category': []},
                      {'category': ''}, {'category': '  '}, {'category': ' 미분류 '}):
            with self.subTest(value=value), patch.object(history_manager,
                    'find_cached_ai_data_by_hash', return_value=value):
                before = self.provider.await_count
                result = await self.process()
                self.assertEqual(self.provider.await_count, before + 1)
                self.assertEqual(result['category'], '복지')
                self.assertFalse(result.get('ai_cache_hit'))
        self.assertEqual(sum(json.loads(self.state.read_text())['calls'].values()), 8)

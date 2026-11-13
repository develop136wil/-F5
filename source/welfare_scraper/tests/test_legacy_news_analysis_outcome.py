"""Legacy failure recovery: synthetic responses and a disposable DB only."""
import asyncio
import datetime
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import ai_classifier
import database
import history_manager
from services import legacy_news_analysis as outcomes, news_unified_feed as feed
from services.scraper_service import ScraperService


class FailureMetadataTests(unittest.TestCase):
    def test_only_exact_old_fallback_is_recovered(self):
        old = dict(outcomes._OLD_FALLBACK)
        self.assertEqual(outcomes.failure(old)['reason'], 'legacy_analysis_failed')
        for valid in (dict(old, is_relevant=False), dict(old, support_detail='상시 제공'),
                      dict(old, category='복지'), dict(old, reasoning='확인됨'),
                      dict(old, custom_result=True), {'category': '미분류'}):
            with self.subTest(valid=valid):
                self.assertIsNone(outcomes.failure(valid))
                self.assertFalse(outcomes.backlog_due({'ai_data': json.dumps(valid)}))

    def test_retry_delay_and_limit(self):
        first = outcomes.failed_result(RuntimeError('private response'), attempts=1)
        self.assertNotIn('private', json.dumps(first))
        self.assertFalse(outcomes.backlog_due({'ai_data': first}))
        future = datetime.datetime.now() + datetime.timedelta(days=1)
        self.assertTrue(outcomes.backlog_due({'ai_data': first}, now=future))
        last = outcomes.failed_result(RuntimeError('private response'), attempts=3)
        self.assertFalse(outcomes.failure(last)['retryable'])
        self.assertFalse(outcomes.backlog_due({'ai_data': last}, now=future))

    def test_malformed_attempts_are_held_without_breaking_backlog(self):
        for value in (None, 'bad', True, -1, 999):
            with self.subTest(value=value):
                result = dict(analysis_status='failed', analysis_failure=dict(
                    attempts=value, retryable=True, reason='analysis_error'))
                self.assertFalse(outcomes.backlog_due({'ai_data': result}))
                self.assertEqual(outcomes.failure(result)['reason'], 'invalid_saved_result')

    def test_daily_quota_reset_uses_pacific_midnight_not_utc_or_local_date(self):
        from services.ai_coordinator import AiDailyQuotaExhausted
        saved = outcomes.failed_result(AiDailyQuotaExhausted('synthetic'), attempts=3)
        meta = outcomes.failure(saved)
        self.assertTrue(meta['retryable'])
        self.assertEqual(meta['attempts'], 2)
        self.assertIsNotNone(datetime.datetime.fromisoformat(meta['next_retry_at']).tzinfo)
        # 23:30 PDT is already the following UTC day. The UTC midnight must not
        # resume this record; the next Pacific midnight is 07:00 UTC.
        legacy = dict(analysis_status='failed', analysis_failure=dict(
            reason='daily_quota_exhausted', retryable=False, attempts=3, next_retry_at=None))
        item = dict(ai_data=json.dumps(legacy), ai_last_attempt_at='2026-09-18T06:30:00+00:00')
        before = dict(item)
        self.assertFalse(outcomes.backlog_due(item, now=datetime.datetime.fromisoformat('2026-09-18T06:59:59+00:00')))
        self.assertTrue(outcomes.backlog_due(item, now=datetime.datetime.fromisoformat('2026-09-18T07:00:00+00:00')))
        self.assertEqual(item, before)
        self.assertEqual(outcomes._next_provider_reset('2026-01-18T07:30:00+00:00').isoformat(),
                         '2026-01-18T08:00:00+00:00')

    def test_daily_recovery_does_not_relax_review_holds_or_unknown_dates(self):
        tomorrow = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
        for reason in ('ai_input_review_required', 'ai_input_size_review_required',
                       'ai_response_review_required', 'ai_source_review_required', 'authentication_required'):
            saved = outcomes.failed_result(reason=reason, attempts=1)
            self.assertFalse(outcomes.backlog_due(dict(ai_data=saved,
                ai_last_attempt_at='2000-01-01T00:00:00+00:00'), now=tomorrow))
        for timestamp in (None, '', 'not-a-date'):
            old = dict(analysis_status='failed', analysis_failure=dict(
                reason='daily_quota_exhausted', retryable=False, attempts=1, next_retry_at=None))
            self.assertFalse(outcomes.backlog_due(dict(ai_data=old, ai_last_attempt_at=timestamp), now=tomorrow))


class ClassifierResultValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_results_are_terminal_review_before_enrichment(self):
        from services.ai_generation_safety import AiResponseReviewRequired
        valid = dict(is_relevant=True, category='복지', target='일반')
        invalid = [{}, {'event_date':'2099-01-01'}, [],
                   dict(valid, is_relevant=1), dict(valid, category=''),
                   dict(valid, target=None), dict(valid, start_age_months=True),
                   dict(valid, start_age_months=120, end_age_months=0),
                   dict(valid, unsupported_number=float('nan'))]
        for result in invalid:
            with self.subTest(result=result), \
                    patch('services.ai_coordinator.input_token_budget', return_value=100000), \
                    patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=result)) as api:
                with self.assertRaises(AiResponseReviewRequired):
                    await ai_classifier.classify_post_async(object(), '합성 안내', 'https://example.invalid',
                        content='공개 복지 안내', custom_prompt='', strict=True)
                api.assert_awaited_once()
                if isinstance(result, dict):
                    self.assertNotIn('source_input', result)
        with patch('services.ai_coordinator.input_token_budget', return_value=100000), \
                patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={})):
            result = await ai_classifier.classify_post_async(object(), '합성 안내', 'https://example.invalid',
                content='공개 복지 안내', custom_prompt='')
        self.assertEqual(outcomes.failure(result)['reason'], 'ai_response_review_required')
        self.assertFalse(outcomes.failure(result)['retryable'])

    async def test_valid_irrelevant_and_undated_results_keep_their_classification(self):
        for relevant in (False, True):
            with self.subTest(relevant=relevant), \
                    patch('services.ai_coordinator.input_token_budget', return_value=100000), \
                    patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={
                        'is_relevant':relevant, 'category':'복지', 'target':'일반'})):
                result = await ai_classifier.classify_post_async(object(), '합성 안내', 'https://example.invalid',
                    content='공개 복지 안내', custom_prompt='', strict=True)
            self.assertIs(result['is_relevant'], relevant)
            self.assertIsNone(outcomes.failure(result))
            self.assertNotIn('event_date', result)


class LegacyOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.close_all_connections()
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, 'DB_FILE', os.path.join(self.temp.name, 'test.db'))
        self.db_patch.start()
        database.init_db()
        self.svc = ScraperService()
        self.svc.ai_rate_limiter.acquire = AsyncMock()
        self.item = dict(site_name='시험 기관', title='교육 지원 신청', link='https://example.invalid/post/1')

    def tearDown(self):
        database.close_all_connections()
        self.db_patch.stop()
        self.temp.cleanup()

    def seed(self, ai_data, **fields):
        item = dict(self.item, ai_data=json.dumps(ai_data), **fields)
        history_manager.save_data([item])

    def processing_patches(self, result=None, error=None):
        stack = ExitStack()
        stack.enter_context(patch.object(ai_classifier, 'fetch_page_content_async',
            new=AsyncMock(return_value=dict(text='교육비 지원을 안내합니다. ' * 70, images=[], attachments=[]))))
        stack.enter_context(patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None))
        self.classify = stack.enter_context(patch.object(ai_classifier, 'classify_post_async',
            new=AsyncMock(return_value=result, side_effect=error)))
        return stack

    async def test_input_budget_failure_is_not_classified_or_retried(self):
        from services.ai_coordinator import AiInputTokenBudgetExceeded
        with self.processing_patches(error=AiInputTokenBudgetExceeded(6001, 6000)):
            result = await self.svc._process_single_item_ai(None, object(), dict(self.item))
        self.classify.assert_awaited_once()
        self.assertFalse(result['is_relevant'])
        self.assertNotIn('ai_call_at', result)
        self.assertEqual(outcomes.failure(result['ai_data'])['reason'], 'ai_input_size_review_required')
        history_manager.save_data([result])
        self.assertEqual(history_manager.get_ai_backlog(5), [])
        with database.get_connection() as conn:
            self.assertEqual(feed.read_feed(conn)['total'], 0)
            diagnostic = feed.read_feed(conn, view='review')['items'][0]
            self.assertFalse(diagnostic['selectable'])
            self.assertEqual(diagnostic['review_reason'], 'ai_input_size_review_required')

    async def test_transient_failure_attempts_are_preserved_and_bounded(self):
        item = dict(self.item)
        with self.processing_patches(error=RuntimeError('offline failure')):
            for attempt in range(1, 4):
                item = await self.svc._process_single_item_ai(None, object(), item)
                self.assertEqual(outcomes.failure(item['ai_data'])['attempts'], attempt)
        self.assertEqual(self.classify.await_count, 3)
        self.assertFalse(outcomes.failure(item['ai_data'])['retryable'])

    async def test_successful_undated_and_irrelevant_remain_successful(self):
        for relevant in (False, True):
            with self.subTest(relevant=relevant), self.processing_patches(result={
                    'category':'복지', 'item_type':'program', 'is_relevant':relevant, 'deadline':None}):
                result = await self.svc._process_single_item_ai(None, object(), dict(self.item))
                self.assertIsNone(outcomes.failure(result['ai_data']))
                self.assertEqual(result['is_relevant'], relevant)
                self.assertFalse(outcomes.backlog_due(result))

    async def test_old_fallback_read_is_nonmutating_and_retry_is_possible(self):
        self.seed(outcomes._OLD_FALLBACK)
        with database.get_connection() as conn:
            before = conn.total_changes
            row = feed.read_feed(conn, view='processing')['items'][0]
            self.assertEqual(row['review_reason'], 'legacy_analysis_failed')
            self.assertEqual(conn.total_changes, before)
        self.assertEqual(history_manager.count_ai_backlog(), 1)
        pending = history_manager.get_ai_backlog(1)[0]
        with self.processing_patches(result={'category':'복지', 'is_relevant':True, 'event_date':'2099-01-01'}):
            result = await self.svc._process_single_item_ai(None, object(), pending)
        history_manager.save_data([result])
        self.assertEqual(history_manager.count_ai_backlog(), 0)
        with database.get_connection() as conn:
            self.assertEqual(feed.read_feed(conn)['total'], 1)

    def run_patches(self, items, *, legacy_daily_limit='150'):
        stack = ExitStack()
        engine = MagicMock(start=AsyncMock(), close=AsyncMock(),
            scrape_site=AsyncMock(return_value={'state':'finished', 'items':items}))
        stack.enter_context(patch('services.scraper_service.ScraperEngine', return_value=engine))
        stack.enter_context(patch('services.scraper_service._acquire_scrape_lock', return_value=object()))
        stack.enter_context(patch('services.scraper_service._release_scrape_lock'))
        stack.enter_context(patch('utils.load_sites', return_value=[{'name':'시험 기관','url':'https://example.invalid'}]))
        stack.enter_context(patch('utils.get_env_value', side_effect=lambda name: {
            'DAILY_AI_LIMIT':legacy_daily_limit,'AI_BACKLOG_RESERVE':'30'}.get(name, '')))
        stack.enter_context(patch.object(ai_classifier, 'get_client', return_value=object()))
        self.usage_status = stack.enter_context(patch('services.ai_coordinator.status', return_value={'models':{}}))
        stack.enter_context(patch('content_filter.is_excluded', return_value=False))
        stack.enter_context(patch('content_filter.get_matched_keywords', return_value=['교육']))
        stack.enter_context(patch('services.relevance_filter.passes_post_ai', return_value=(True,'')))
        stack.enter_context(patch.object(history_manager, 'run_maintenance'))
        stack.enter_context(patch.dict(os.environ, {'SCRAPE_INTERVAL_HOURS':'0'}))
        self.save_ready = stack.enter_context(patch.object(self.svc, '_save_results',
            new=AsyncMock(side_effect=lambda items, history: items)))
        return stack

    async def test_failed_candidate_saved_only_in_diagnostics_and_partial(self):
        progress = MagicMock()
        with self.run_patches([dict(self.item)]), self.processing_patches(error=RuntimeError('offline failure')):
            found = await self.svc.run_scraper_async(progress_callback=progress)
        self.assertEqual(found, [])
        self.save_ready.assert_not_awaited()
        self.assertEqual(self.svc.last_run_outcome['state'], 'partial_failure')
        self.assertEqual(self.svc.last_ai_savings['analyzed'], 0)
        self.assertEqual(self.svc.last_ai_savings['analysis_failed'], 1)
        self.assertEqual(self.svc.last_ai_savings['analysis_deferred'], 0)
        self.assertIn('AI 분석 실패 1건', self.svc.last_run_outcome['message'])
        with database.get_connection() as conn:
            self.assertEqual(feed.read_feed(conn, view='processing')['total'], 1)

    async def test_backlog_recovery_runs_even_without_new_scraped_candidates(self):
        self.seed(outcomes._OLD_FALLBACK)
        with self.run_patches([]), self.processing_patches(result={
                'category':'복지', 'is_relevant':True, 'event_date':'2099-01-01'}):
            found = await self.svc.run_scraper_async()
        self.assertEqual(len(found), 1)
        self.assertEqual(history_manager.count_ai_backlog(), 0)
        self.assertEqual(self.svc.last_ai_savings['backlog_completed'], 1)
        self.assertEqual(self.svc.last_run_outcome['state'], 'completed')

    async def test_new_candidates_ignore_legacy_daily_limit_and_historical_counts(self):
        items = [dict(self.item, title=f'교육 지원 신청 {number}',
                      link=f'https://example.invalid/new/{number}') for number in range(25)]
        with self.run_patches(items, legacy_daily_limit='0'), self.processing_patches(result={
                'category':'복지', 'is_relevant':True, 'event_date':'2099-01-01'}), \
                patch.object(history_manager, 'get_daily_ai_count', return_value=500) as old_count:
            found = await self.svc.run_scraper_async()
        old_count.assert_not_called()
        self.assertEqual(len(found), 25)
        self.assertEqual(self.classify.await_count, 25)
        self.assertEqual(self.svc.last_ai_savings['quota_skipped'], 0)
        self.assertEqual(self.svc.last_ai_savings['analysis_deferred'], 0)
        self.assertEqual(self.svc.last_run_outcome['state'], 'completed')

    async def test_backlog_recovery_ignores_legacy_daily_limit_and_historical_counts(self):
        self.seed(outcomes._OLD_FALLBACK)
        with self.run_patches([], legacy_daily_limit='0'), self.processing_patches(result={
                'category':'복지', 'is_relevant':True, 'event_date':'2099-01-01'}), \
                patch.object(history_manager, 'get_daily_ai_count', return_value=500) as old_count:
            found = await self.svc.run_scraper_async()
        old_count.assert_not_called()
        self.classify.assert_awaited_once()
        self.assertEqual(len(found), 1)
        self.assertEqual(history_manager.count_ai_backlog(), 0)
        self.assertEqual(self.svc.last_ai_savings['backlog_completed'], 1)

    async def test_provider_daily_exhaustion_keeps_original_without_false_found_result(self):
        from services.ai_coordinator import AiDailyQuotaExhausted
        with self.run_patches([dict(self.item)], legacy_daily_limit='0'), \
                self.processing_patches(error=AiDailyQuotaExhausted('synthetic provider hold')):
            found = await self.svc.run_scraper_async()
        self.assertEqual(found, [])
        self.save_ready.assert_not_awaited()
        self.assertEqual(self.svc.last_run_outcome['state'], 'partial_failure')
        self.assertEqual(self.svc.last_ai_savings['analysis_failed'], 1)
        with database.get_connection() as conn:
            saved = dict(conn.execute('SELECT * FROM welfare_items WHERE link=?', (self.item['link'],)).fetchone())
            self.assertEqual(outcomes.failure(saved['ai_data'])['reason'], 'daily_quota_exhausted')
            self.assertEqual(saved['title'], self.item['title'])

    async def test_known_provider_hold_skips_fetch_and_preserves_new_candidates(self):
        items = [dict(self.item, link=f'https://example.invalid/held/{number}') for number in range(4)]
        with self.run_patches(items), self.processing_patches(result={'category':'복지'}) as _stack, \
                patch.object(ai_classifier, 'fetch_page_content_async', new_callable=AsyncMock) as fetch:
            self.usage_status.return_value = {'models':{'gemini-3.5-flash-lite':{'daily_exhausted':True}}}
            found = await self.svc.run_scraper_async()
        fetch.assert_not_awaited()
        self.classify.assert_not_awaited()
        self.assertEqual(found, [])
        self.assertEqual(self.svc.last_run_outcome['state'], 'partial_failure')
        self.assertEqual(history_manager.count_ai_backlog(), 0)
        with database.get_connection() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM welfare_items')]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(outcomes.failure(row['ai_data'])['reason'] == 'daily_quota_exhausted' for row in rows))
        self.assertTrue(all(outcomes.failure(row['ai_data'])['attempts'] == 0 for row in rows))

    async def test_old_terminal_daily_record_recovers_after_reset_without_rediscovery(self):
        old = dict(analysis_status='failed', analysis_failure=dict(
            reason='daily_quota_exhausted', retryable=False, attempts=3, next_retry_at=None))
        self.seed(old, ai_last_attempt_at='2000-01-01T00:00:00+00:00', ai_skip_reason='analysis_review_required')
        self.assertEqual(history_manager.count_ai_backlog(), 1)
        with self.run_patches([]), self.processing_patches(result={
                'category':'복지', 'is_relevant':True, 'event_date':'2099-01-01'}):
            found = await self.svc.run_scraper_async()
        self.classify.assert_awaited_once()
        self.assertEqual(len(found), 1)
        self.assertEqual(history_manager.count_ai_backlog(), 0)

    async def test_unknown_usage_state_skips_fetch_without_mislabeling_privacy(self):
        with self.run_patches([dict(self.item)]), \
                patch.object(ai_classifier, 'fetch_page_content_async', new_callable=AsyncMock) as fetch:
            self.usage_status.side_effect = RuntimeError('synthetic usage failure')
            self.assertEqual(await self.svc.run_scraper_async(), [])
        fetch.assert_not_awaited()
        with database.get_connection() as conn:
            row = dict(conn.execute('SELECT * FROM welfare_items').fetchone())
        held = outcomes.failure(row['ai_data'])
        self.assertEqual(held['reason'], 'ai_usage_state_unavailable')
        self.assertEqual(held['attempts'], 0)
        self.assertTrue(held['retryable'])
        self.assertFalse(outcomes.backlog_due(row))
        self.assertTrue(outcomes.backlog_due(row, now=datetime.datetime.fromisoformat(held['next_retry_at'])))

    async def test_analysis_body_concurrency_is_bounded_and_result_order_is_preserved(self):
        items = [dict(self.item, link=f'https://example.invalid/bounded/{number}') for number in range(12)]
        active = peak = 0
        async def analyze(_session, _client, item, *_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                return dict(item, ai_data=json.dumps({'category':'복지'}), is_relevant=True)
            finally:
                active -= 1
        with self.run_patches(items), patch.object(self.svc, '_process_single_item_ai', side_effect=analyze):
            found = await self.svc.run_scraper_async()
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertEqual([item['link'] for item in found], [item['link'] for item in items])

    async def test_cancelled_collection_cancels_workers_without_starting_waiting_bodies(self):
        items = [dict(self.item, link=f'https://example.invalid/cancel/{number}') for number in range(12)]
        started, active = [], 0
        three_started = asyncio.Event()
        hold = asyncio.Event()
        stop = asyncio.Event()
        async def analyze(_session, _client, item, *_args):
            nonlocal active
            active += 1
            started.append(item['link'])
            if len(started) == 3:
                three_started.set()
            try:
                await hold.wait()
                return item
            finally:
                active -= 1
        with self.run_patches(items), patch.object(self.svc, '_process_single_item_ai', side_effect=analyze):
            task = asyncio.create_task(self.svc.run_scraper_async(stop_event=stop))
            try:
                await asyncio.wait_for(three_started.wait(), timeout=5)
                stop.set()
                self.assertEqual(await asyncio.wait_for(task, timeout=5), [])
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(started), 3)
        self.assertEqual(active, 0)
        self.assertEqual(self.svc.last_run_outcome['state'], 'cancelled')

    async def test_api_terminal_status_keeps_partial_outcome(self):
        import api_server
        self.svc.run_scraper_async = AsyncMock(return_value=[])
        self.svc.last_run_outcome = {'state':'partial_failure', 'message':'일부 미완료 — AI 분석 실패 1건'}
        with patch('services.scraper_service.ScraperService', return_value=self.svc), \
                patch.object(api_server, 'STOP_EVENT', asyncio.Event()), \
                patch.dict(api_server.SCRAPER_STATUS, {}, clear=True), \
                patch('services.ai_coordinator.finish_job'):
            await api_server.run_scraper_task(True)
            self.assertEqual(api_server.SCRAPER_STATUS['status_code'], 'partial_failure')
            self.assertIn('AI 분석 실패', api_server.SCRAPER_STATUS['message'])

    async def test_source_date_recheck_cannot_bypass_terminal_retry_limit(self):
        self.seed(outcomes.failed_result(RuntimeError('offline'), attempts=3),
                  scraped_at='2000-01-01T00:00:00')
        with self.run_patches([dict(self.item)]), self.processing_patches(error=RuntimeError('must not run')):
            self.assertEqual(await self.svc.run_scraper_async(), [])
        self.classify.assert_not_awaited()

    async def test_incomplete_backlog_without_ai_result_is_partial(self):
        self.seed({})
        async def deferred(_session, _client, item, *_args):
            return dict(item, ai_last_attempt_at=datetime.datetime.now().isoformat())
        with self.run_patches([]), patch.object(self.svc, '_process_single_item_ai', side_effect=deferred):
            self.assertEqual(await self.svc.run_scraper_async(), [])
        self.assertEqual(self.svc.last_run_outcome['state'], 'partial_failure')
        self.assertEqual(self.svc.last_ai_savings['analysis_deferred'], 1)

    async def test_malformed_dated_response_is_not_found_or_feed_ready(self):
        with self.run_patches([dict(self.item)]), \
                patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value={
                    'text':'공개 복지 안내 ' * 100, 'images':[], 'attachments':[]})), \
                patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None), \
                patch('services.ai_coordinator.input_token_budget', return_value=100000), \
                patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={
                    'event_date':'2099-01-01'})) as api:
            found = await self.svc.run_scraper_async()
        api.assert_awaited_once()
        self.assertEqual(found, [])
        self.save_ready.assert_not_awaited()
        self.assertEqual(self.svc.last_run_outcome['state'], 'partial_failure')
        self.assertEqual(self.svc.last_ai_savings['analysis_failed'], 1)
        self.assertEqual(history_manager.get_ai_backlog(10), [])
        with database.get_connection() as conn:
            self.assertEqual(feed.read_feed(conn)['total'], 0)
            held = feed.read_feed(conn, view='review')['items']
            self.assertEqual(len(held), 1)
            self.assertEqual(held[0]['review_reason'], 'ai_response_review_required')


if __name__ == '__main__':
    unittest.main()

import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import app_lifecycle, news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services.news_site_collector import collect_registered_sites


def tearDownModule():
    import database
    database.close_all_connections()


class Engine:
    def __init__(self, *, headless):
        self.headless = headless
        self.start = AsyncMock()
        self.close = AsyncMock()
        self.scrape_site = AsyncMock(return_value={'state':'finished','items':[
            {'title': '노년 지원', 'link': 'https://example.invalid/1', 'ignored': 'x'},
            {'title': '돌봄 소식', 'link': 'https://example.invalid/2'}]})


class TestSiteCollector(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_lifecycle.shutdown_requested = False
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn); jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p', 1, '노년', '전국', keywords=('지원',),
            site_ids=('1',), semantic_text='노년 돌봄'))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='고정 사이트', url='https://example.invalid', selectors={})])
        self.engine = Engine(headless=True)
        self.acquire = patch('services.scraper_service._acquire_scrape_lock', return_value=object()).start()
        self.release = patch('services.scraper_service._release_scrape_lock').start()

    def tearDown(self):
        patch.stopall(); self.conn.close(); app_lifecycle.shutdown_requested = False

    async def db(self, fn, *args, **kwargs): return fn(self.conn, *args, **kwargs)
    async def collect(self, fetcher=None, **kwargs):
        return await collect_registered_sites(self.run, analysis_model='fixed', call_db=self.db,
            engine_factory=lambda **kw: self.engine, content_fetcher=fetcher or AsyncMock(return_value={'text':'본문'}),
            **kwargs)

    async def test_collects_snapshot_site_and_registers_without_legacy_write(self):
        summary = await self.collect()
        self.assertEqual(summary, {'state':'finished','sites':1,'registered':2,'content_failed':0,'saved_total':2})
        self.engine.scrape_site.assert_awaited_once()
        site = self.engine.scrape_site.call_args.args[0]
        self.assertEqual(site['name'], '고정 사이트')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 1)
        self.engine.close.assert_awaited_once(); self.release.assert_called_once()

    async def test_content_failure_keeps_title_candidate(self):
        summary = await self.collect(AsyncMock(side_effect=RuntimeError('private failure')))
        self.assertEqual((summary['registered'], summary['content_failed']), (2, 2))
        payloads = [r[0] for r in self.conn.execute('SELECT payload_json FROM news_run_candidates')]
        self.assertTrue(all('private failure' not in p for p in payloads))

    async def test_deferred_analysis_collects_every_site_without_jobs_or_duplicate_bodies(self):
        second_profile = NewsProfile('two-sites',1,'원문 수집','전국',keywords=('지원',),site_ids=('1','2'))
        profiles.create_profile(self.conn,second_profile)
        self.run = profiles.prepare_run(self.conn,'two-sites',expected_revision=1,sites=[
            dict(id=1,name='첫 기관',url='https://first.example.invalid',selectors={}),
            dict(id=2,name='둘째 기관',url='https://second.example.invalid',selectors={})])
        fetcher = AsyncMock(return_value={'text':'수집한 원문'})
        with patch('ai_classifier.get_client', side_effect=AssertionError('No AI client during source collection')):
            first = await self.collect(fetcher,defer_analysis=True)
            again = await self.collect(fetcher,defer_analysis=True)
        self.assertEqual((first['sites'],first['registered'],again['registered']),(2,4,0))
        self.assertEqual(fetcher.await_count,4)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT DISTINCT route FROM news_run_candidates').fetchall(),[('analysis_unconfigured',)])

    async def test_screen_review_preserves_links_and_counts_sites_not_missing_links(self):
        self.engine.scrape_site.return_value['vision_review_required'] = True
        fetcher = AsyncMock(return_value={'text':'본문'})
        result = await self.collect(fetcher)
        self.assertEqual((result['state'], result['registered'], result['vision_review_sites']), ('finished',2,1))
        self.assertEqual(result.get('skipped_candidates',0), 0)
        self.engine.scrape_site.return_value['vision_review_required'] = False
        again = await self.collect(fetcher)
        self.assertEqual((again['registered'], again['vision_review_sites']), (0,1))
        self.assertEqual(fetcher.await_count, 2)
        self.assertEqual(self.conn.execute('SELECT issue_key FROM news_collection_issues').fetchall(), [('vision_review_required',)])

    async def test_zero_links_with_screen_review_is_distinct_and_survives_database_reopen(self):
        import tempfile
        from pathlib import Path
        self.engine.scrape_site.return_value = {'state':'finished','items':[], 'vision_review_required':False}
        genuine = await self.collect()
        self.assertEqual(genuine.get('vision_review_sites',0),0)
        self.engine.scrape_site.return_value['vision_review_required'] = True
        result = await self.collect()
        self.assertEqual((result['state'], result['registered'], result['vision_review_sites']), ('finished',0,1))
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary)/'synthetic-vision.db')
            stored = sqlite3.connect(path); self.conn.backup(stored)
            self.conn.close(); stored.close(); self.conn = sqlite3.connect(path)
            again = await self.collect()
            self.assertEqual(again['vision_review_sites'],1)
            self.assertEqual(again.get('skipped_candidates',0),0)
            self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.conn.close(); self.conn = sqlite3.connect(':memory:')

    async def test_screen_review_write_failure_is_not_success_and_not_retried(self):
        self.engine.scrape_site.return_value = {'state':'finished','items':[], 'vision_review_required':True}
        with patch('services.news_site_collector._record_issues', side_effect=sqlite3.OperationalError('synthetic')) as record:
            result = await self.collect()
        self.assertEqual((result['state'],result['stop_reason']),('waiting','site_save_failed'))
        record.assert_called_once()
        self.assertEqual((await self.collect())['vision_review_sites'],1)

    async def test_page_coverage_survives_collection_queue_worker_and_results(self):
        import json
        from services.page_coverage import make
        from services.news_analysis_worker import process_job
        page = {'text': '지원 안내', 'source_page': make('page_char_limit')}
        await self.collect(AsyncMock(return_value=page))
        job_id = self.conn.execute('SELECT job_id FROM news_analysis_jobs').fetchone()[0]
        facts = dict(is_relevant=True, category='복지', target='노년', residence=[], life_cycle=[],
                     target_characteristics=[], interest_topics=[], start_age_months=None,
                     end_age_months=None, evidence_snippet='',
                     applicant_type='unknown', applicant_detail='미상', beneficiary_detail='미상',
                     applicant_evidence='', life_cycle_evidence=[], residence_evidence=[],
                     _support_source_units=[1], eligibility_detail='', contact='', deadline='',
                     fact_evidence={key: '' for key in
                         ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
        # This fixture uses the synthetic model "fixed" to test result/coverage
        # transport, not live model eligibility. Give its mocked dispatch an
        # explicit planning allowance; real model admission has separate tests.
        with patch('services.ai_coordinator.input_token_budget', return_value=6000), \
                patch('services.ai_coordinator.input_token_capacity', return_value=6000), \
                patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value=facts)) as ai:
            self.assertEqual(await process_job(job_id, object(), call_db=self.db), 'complete')
        ai.assert_awaited_once()
        result = json.loads(jobs.get_job(self.conn, job_id)['result_json'])
        self.assertFalse(result['accepted'])
        self.assertTrue(result['review_required'])
        saved = result['analysis']
        self.assertEqual(saved['support_detail'], page['text'])
        self.assertEqual(saved['source_page'], page['source_page'])
        visible = jobs.list_profile_results(self.conn, 'p')['items']
        self.assertTrue(any('뒷부분' in row['input_note'] for row in visible))

    async def test_malformed_response_does_not_become_complete_page_result(self):
        from services.page_coverage import make
        from services.news_analysis_worker import process_job
        await self.collect(AsyncMock(return_value={
            'text': '지원 안내', 'source_page': make('page_char_limit')}))
        job_id = self.conn.execute('SELECT job_id FROM news_analysis_jobs').fetchone()[0]
        with patch('services.ai_coordinator.input_token_budget', return_value=6000), \
                patch('services.ai_coordinator.input_token_capacity', return_value=6000), \
                patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value={})) as ai:
            self.assertEqual(await process_job(job_id, object(), call_db=self.db), 'failed')
        ai.assert_awaited_once()
        saved = jobs.get_job(self.conn, job_id)
        self.assertEqual(saved['reason'], 'invalid_analysis')
        self.assertIsNone(saved['result_json'])

    async def test_local_attachment_text_is_included_in_profile_candidate(self):
        async def enrich(_session, page):
            return dict(page, attachment_text='첨부파일에서 읽은 지원 대상과 신청 기간',
                        attachment_notices=[], attachment_reports=[], attachment_fingerprint='abc')
        fetcher = AsyncMock(return_value={
            'text': '짧은 게시판 본문', 'images': [], 'attachments': [], 'attachment_items': []})
        with patch('services.news_site_collector.news_attachments.enrich_page_locally', side_effect=enrich):
            await self.collect(fetcher)
        payloads = [r[0] for r in self.conn.execute('SELECT payload_json FROM news_run_candidates')]
        self.assertTrue(all('첨부파일에서 읽은 지원 대상과 신청 기간' in value for value in payloads))

    async def test_attachment_notice_without_evidence_remains_content_failure(self):
        async def enrich(_session, page):
            return dict(page, text='', attachment_text='',
                        attachment_notices=['[첨부파일 상태: 직접 확인 필요 - 신청서.hwp]'],
                        attachment_reports=[], attachment_fingerprint='')
        fetcher = AsyncMock(return_value={
            'text': '', 'images': [], 'attachments': ['https://example.invalid/a.hwp']})
        with patch('services.news_site_collector.news_attachments.enrich_page_locally', side_effect=enrich):
            summary = await self.collect(fetcher)
        self.assertEqual(summary['content_failed'], 2)
        import json
        for row in self.conn.execute('SELECT payload_json FROM news_run_candidates'):
            self.assertIn('attachment_review', json.loads(row[0])['source_page']['reasons'])

    async def test_busy_and_shutdown_do_not_start_engine(self):
        self.acquire.return_value = None
        self.assertEqual((await self.collect())['state'], 'busy')
        self.engine.start.assert_not_awaited()
        self.acquire.return_value = object(); app_lifecycle.shutdown_requested = True
        self.assertEqual((await self.collect())['state'], 'shutdown')
        self.engine.start.assert_not_awaited()

    async def test_cancel_closes_engine_and_releases_lock(self):
        entered = asyncio.Event()
        async def fetch(*args): entered.set(); await asyncio.Event().wait()
        task = asyncio.create_task(self.collect(fetch))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.engine.close.assert_awaited_once(); self.release.assert_called_once()

    async def test_invalid_items_are_skipped_and_duplicates_do_not_expand_batch(self):
        self.engine.scrape_site.return_value = {'state':'finished','items':
            [{'title':'지원','link':'https://example.invalid'}] * 101 + [{'bad':1}]}
        summary = await self.collect()
        self.assertEqual(summary['registered'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 1)

    async def test_invalid_rows_do_not_consume_valid_candidate_limit(self):
        invalid = [{'bad': i} for i in range(100)]
        valid = [{'title': f'지원 {i}', 'link': f'https://example.invalid/{i}'} for i in range(100)]
        self.engine.scrape_site.return_value = {'state':'finished','items':invalid + valid}
        summary = await self.collect()
        self.assertEqual(summary['registered'], 100)

    async def test_linkless_and_unsafe_posts_are_visible_without_fetching_or_retrying_them(self):
        from scraper_engine import _clean_items
        invalid = _clean_items([dict(site_name='시험', title='지원 링크 없는 게시글', link='')])
        invalid += [dict(title='지원', link=link) for link in (
            'javascript:open(1)', 'http://127.0.0.1/private', 'https://user:secret@example.invalid')]
        valid = dict(title='지원 원문', link='https://example.invalid/valid')
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': invalid + invalid + [valid]}
        fetcher = AsyncMock(return_value={'text': '본문'})
        result = await self.collect(fetcher)
        self.assertEqual((result['state'], result['skipped_candidates'], result['registered']), ('finished', 4, 1))
        fetcher.assert_awaited_once()
        self.assertEqual(fetcher.call_args.args[1], valid['link'])
        result = await self.collect(fetcher)
        self.assertEqual((result['state'], result['skipped_candidates'], result['registered']), ('finished', 4, 0))
        self.assertEqual(fetcher.await_count, 1)
        rows = self.conn.execute('SELECT * FROM news_collection_issues').fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row[2]) == 64 for row in rows))
        self.assertNotIn('secret', str(rows))
        self.assertNotIn('javascript', str(rows))

    async def test_unusable_only_source_is_distinct_from_genuinely_empty_source(self):
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': []}
        empty = await self.collect()
        self.assertEqual((empty['state'], empty.get('skipped_candidates', 0), empty['saved_total']), ('finished', 0, 0))
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': [{'title': '지원 안내', 'link': ''}]}
        fetcher = AsyncMock()
        skipped = await self.collect(fetcher)
        self.assertEqual((skipped['state'], skipped['skipped_candidates'], skipped['saved_total']), ('finished', 1, 0))
        fetcher.assert_not_awaited()

    async def test_unusable_receipts_survive_reopen_and_partial_collection_resume(self):
        import tempfile
        from pathlib import Path
        self.engine.scrape_site.return_value = {'state': 'partial', 'items': [{'title': '지원 안내', 'link': ''}]}
        first = await self.collect()
        self.assertEqual((first['state'], first['skipped_candidates']), ('finished', 1))
        self.assertEqual(first['site_partial'], 1)
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / 'issues.db')
            stored = sqlite3.connect(path)
            self.conn.backup(stored)
            self.conn.close(); stored.close()
            self.conn = sqlite3.connect(path)
            self.engine.scrape_site.return_value['state'] = 'finished'
            result = await self.collect()
            self.assertEqual((result['state'], result['skipped_candidates']), ('finished', 1))
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_collection_issues').fetchone()[0], 1)
            self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.conn.close()
            self.conn = sqlite3.connect(':memory:')

    async def test_issue_write_failure_does_not_claim_success_or_retry_automatically(self):
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': [{'title': '지원 안내', 'link': ''}]}
        with patch('services.news_site_collector._record_issues', side_effect=sqlite3.OperationalError('private write error')) as record:
            result = await self.collect()
        self.assertEqual((result['state'], result['stop_reason']), ('waiting', 'site_save_failed'))
        record.assert_called_once()
        self.assertNotIn('private', str(result))
        recovered = await self.collect()
        self.assertEqual((recovered['state'], recovered['skipped_candidates']), ('finished', 1))

    async def test_cancel_during_issue_write_drains_warning_before_releasing_lock(self):
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': [{'title': '지원 안내', 'link': ''}]}
        started, finish = asyncio.Event(), asyncio.Event()
        async def db(fn, *args, **kwargs):
            if fn.__name__ == '_record_issues':
                started.set()
                await finish.wait()
            return fn(self.conn, *args, **kwargs)
        task = asyncio.create_task(collect_registered_sites(self.run, analysis_model='fixed', call_db=db,
            engine_factory=lambda **kw: self.engine, content_fetcher=AsyncMock()))
        await started.wait(); task.cancel(); await asyncio.sleep(0)
        self.release.assert_not_called()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_collection_issues').fetchone()[0], 1)
        result = await self.collect()
        self.assertEqual((result['state'], result['skipped_candidates']), ('finished', 1))

    async def test_cancel_during_engine_close_is_propagated_after_cleanup(self):
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        async def close():
            close_started.set()
            await close_release.wait()
        self.engine.close.side_effect = close
        task = asyncio.create_task(self.collect())
        await close_started.wait()
        task.cancel()
        close_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.assert_called_once()

    def many_items(self, count):
        return {'state':'finished','items':[
            dict(title=f'지원 {i}', link=f'https://example.invalid/{i}') for i in range(count)]}

    async def test_production_collects_101st_and_later_items_in_small_durable_batches(self):
        self.engine.scrape_site.return_value = self.many_items(143)
        from services.news_candidate_intake import register_candidates
        sizes = []
        def register(conn, run_id, site_id, candidates, **kwargs):
            sizes.append(len(candidates))
            return register_candidates(conn, run_id, site_id, candidates, **kwargs)
        with patch('services.news_site_collector.register_candidates', side_effect=register):
            summary = await self.collect()
        self.assertEqual((summary['state'], summary['registered'], summary['saved_total']), ('finished', 143, 143))
        self.assertEqual(sizes, [20] * 7 + [3])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 143)
        self.assertEqual(self.engine.scrape_site.call_args.kwargs['known_links'], set())
        self.assertTrue(self.engine.scrape_site.call_args.kwargs['with_status'])

    async def test_explicit_sample_limit_is_still_bounded_and_visible(self):
        self.engine.scrape_site.return_value = self.many_items(101)
        summary = await self.collect(limit_per_site=3)
        self.assertEqual((summary['registered'], summary['sampled'], summary['sample_limit']), (3, True, 3))
        for invalid in (0, -1, 101, True, '3'):
            with self.assertRaises(ValueError):
                await self.collect(limit_per_site=invalid)

    async def test_same_run_receipts_skip_fetch_even_if_body_changes(self):
        self.engine.scrape_site.return_value = self.many_items(101)
        await self.collect()
        fetcher = AsyncMock(return_value={'text': '바뀐 본문'})
        summary = await self.collect(fetcher)
        self.assertEqual((summary['registered'], summary['saved_total']), (0, 101))
        fetcher.assert_not_awaited()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 101)

    async def test_receipts_survive_database_reopen(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / 'news-checkpoint.db')
            stored = sqlite3.connect(path)
            self.conn.backup(stored)
            self.conn.close()
            self.conn = stored
            self.engine.scrape_site.return_value = self.many_items(43)
            await self.collect(limit_per_site=20)
            self.conn.close()
            self.conn = sqlite3.connect(path)
            fetcher = AsyncMock(return_value={'text':'바뀐 본문'})
            summary = await self.collect(fetcher)
            self.assertEqual((summary['registered'], summary['saved_total'], fetcher.await_count), (23, 43, 23))
            self.conn.close()
            self.conn = sqlite3.connect(':memory:')

    async def test_later_site_failure_keeps_previous_site_and_resumes_only_unsaved_identities(self):
        profiles.create_profile(self.conn, NewsProfile('two', 1, '기관 둘', '전국',
            site_ids=('1','2')))
        self.run = profiles.prepare_run(self.conn, 'two', expected_revision=1,
            sites=[dict(id=i,name=f'기관 {i}',url=f'https://example.invalid/site/{i}') for i in (1,2)])
        self.engine.scrape_site.side_effect = [self.many_items(21), RuntimeError('private site error')]
        summary = await self.collect()
        self.assertEqual((summary['state'], summary['sites'], summary['saved_total']), ('finished', 1, 21))
        self.assertEqual(summary['site_failed'], 1)
        self.assertEqual(summary['site_issues'], [
            dict(site_id='2', site_name='기관 2', issue_code='site_read_failed')])
        self.engine.scrape_site.side_effect = [self.many_items(21), self.many_items(2)]
        fetcher = AsyncMock(return_value={'text':'본문'})
        summary = await self.collect(fetcher)
        self.assertEqual((summary['state'], summary['registered'], summary['saved_total'], fetcher.await_count),
                         ('finished', 2, 23, 2))
        self.assertNotIn('site_issues', summary)

    async def test_content_failures_across_batches_remain_visible_without_losing_candidates(self):
        self.engine.scrape_site.return_value = self.many_items(101)
        summary = await self.collect(AsyncMock(side_effect=RuntimeError('private content error')))
        self.assertEqual((summary['registered'], summary['content_failed'], summary['saved_total']), (101, 101, 101))

    async def test_cancel_during_fetch_preserves_small_pending_batch_and_resume_skips_it(self):
        self.engine.scrape_site.return_value = self.many_items(43)
        entered = asyncio.Event()
        calls = 0
        async def fetch(*_):
            nonlocal calls
            calls += 1
            if calls == 24:
                entered.set()
                await asyncio.Event().wait()
            return {'text': '본문'}
        task = asyncio.create_task(self.collect(fetch))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 23)
        fetcher = AsyncMock(return_value={'text': '새 본문'})
        summary = await self.collect(fetcher)
        self.assertEqual((summary['registered'], summary['saved_total'], fetcher.await_count), (20, 43, 20))

    async def test_cancel_during_save_drains_receipt_before_unlock(self):
        self.engine.scrape_site.return_value = self.many_items(43)
        started, finish = asyncio.Event(), asyncio.Event()
        async def db(fn, *args, **kwargs):
            if fn.__name__ == 'register_candidates':
                started.set()
                await finish.wait()
            return fn(self.conn, *args, **kwargs)
        task = asyncio.create_task(collect_registered_sites(self.run, analysis_model='fixed', call_db=db,
            engine_factory=lambda **kw: self.engine, content_fetcher=AsyncMock(return_value={'text':'본문'})))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.release.assert_not_called()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 20)
        summary = await self.collect()
        self.assertEqual((summary['registered'], summary['saved_total']), (23, 43))

    async def test_failed_batch_waits_without_automatic_retry_or_duplicate_on_resume(self):
        self.engine.scrape_site.return_value = self.many_items(43)
        from services.news_candidate_intake import register_candidates
        calls = 0
        def register(conn, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError('private DB failure')
            return register_candidates(conn, *args, **kwargs)
        with patch('services.news_site_collector.register_candidates', side_effect=register):
            summary = await self.collect()
        self.assertEqual((summary['state'], summary['stop_reason']), ('waiting', 'site_save_failed'))
        self.assertEqual((calls, summary['registered'], summary['saved_total'], summary['sites']), (2, 20, 20, 0))
        self.assertNotIn('private', str(summary))
        summary = await self.collect()
        self.assertEqual((summary['state'], summary['registered'], summary['saved_total']), ('finished', 23, 43))

    async def test_site_read_error_or_malformed_response_is_not_empty_success(self):
        for result in (None, [], {'error': 'private failure'}, {'state':'finished'},
                       {'state':'unknown','items':[]}):
            self.engine.scrape_site.return_value = result
            summary = await self.collect()
            self.assertEqual((summary['state'], summary['sites']), ('finished', 0))
            self.assertEqual(summary['site_issues'][0]['issue_code'], 'site_response_invalid')
            self.assertEqual(summary['site_failed'], 1)
        self.engine.scrape_site.side_effect = RuntimeError('private remote failure')
        summary = await self.collect()
        self.assertEqual(summary['site_issues'][0]['issue_code'], 'site_read_failed')
        self.assertNotIn('private', str(summary))

    async def test_partial_engine_result_is_saved_and_marked_for_review(self):
        outcome = self.many_items(23)
        for state in ('partial', 'failed'):
            self.engine.scrape_site.return_value = dict(outcome, state=state)
            summary = await self.collect()
            self.assertEqual((summary['state'], summary['sites'], summary['saved_total']), ('finished', 0, 23))
            self.assertEqual(summary['site_review_required'], 1)
            self.assertEqual(summary['site_issues'][0]['issue_code'],
                             'site_partial' if state == 'partial' else 'site_read_failed')
        self.engine.scrape_site.return_value = self.many_items(43)
        summary = await self.collect()
        self.assertEqual((summary['state'], summary['registered'], summary['saved_total']), ('finished', 20, 43))
        self.assertNotIn('site_issues', summary)

    def prepare_two_sites(self):
        profiles.create_profile(self.conn, NewsProfile('two-recovery', 1, '기관 둘', '전국',
            site_ids=('1', '2')))
        self.run = profiles.prepare_run(self.conn, 'two-recovery', expected_revision=1,
            sites=[dict(id=i, name=f'기관 {i}', url=f'https://example.invalid/site/{i}') for i in (1, 2)])

    async def test_failed_first_site_does_not_block_good_second_site_or_leak_error(self):
        self.prepare_two_sites()
        for failure in (RuntimeError('private url credential'),
                        {'state': 'failed', 'items': []}, {'error': 'private malformed'}):
            self.engine.scrape_site.side_effect = [failure, self.many_items(2)]
            summary = await self.collect()
            self.assertEqual((summary['state'], summary['sites'], summary['saved_total']),
                             ('finished', 1, 2))
            self.assertEqual(summary['site_failed'], 1)
            self.assertEqual(summary['site_issues'][0]['site_id'], '1')
            self.assertNotIn('private', str(summary))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 2)

    async def test_partial_first_site_with_screen_review_does_not_block_next_site(self):
        self.prepare_two_sites()
        self.engine.scrape_site.side_effect = [
            dict(self.many_items(2), state='partial', vision_review_required=True), self.many_items(3)]
        with patch('ai_classifier.get_client', side_effect=AssertionError('Do not send screen images to AI')):
            summary = await self.collect(defer_analysis=True)
        self.assertEqual((summary['state'], summary['sites'], summary['registered']), ('finished', 1, 5))
        self.assertEqual((summary['site_partial'], summary['vision_review_sites']), (1, 1))
        self.assertEqual(self.engine.scrape_site.await_count, 2)

    async def test_site_status_survives_restart_and_is_cleared_only_after_successful_read(self):
        import tempfile
        from pathlib import Path
        self.engine.scrape_site.return_value = {'state': 'failed', 'items': []}
        self.assertEqual((await self.collect())['site_failed'], 1)
        with tempfile.TemporaryDirectory() as temporary:
            stored = sqlite3.connect(str(Path(temporary) / 'site-issues.db'))
            self.conn.backup(stored)
            self.conn.close(); self.conn = stored
            self.engine.start.side_effect = RuntimeError('synthetic unavailable browser')
            paused = await self.collect()
            self.assertEqual((paused['state'], paused['site_failed']), ('waiting', 1))
            self.engine.start.side_effect = None
            self.engine.scrape_site.return_value = self.many_items(2)
            recovered = await self.collect()
            self.assertNotIn('site_issues', recovered)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_collection_site_issues').fetchone()[0], 0)
            self.conn.close(); self.conn = sqlite3.connect(':memory:')

    async def test_candidate_write_failure_still_blocks_later_sites(self):
        self.prepare_two_sites()
        with patch('services.news_site_collector.register_candidates',
                   side_effect=sqlite3.OperationalError('private DB failure')) as save:
            summary = await self.collect()
        self.assertEqual((summary['state'], summary['stop_reason']), ('waiting', 'site_save_failed'))
        self.assertEqual(self.engine.scrape_site.await_count, 1)
        save.assert_called_once()
        self.assertNotIn('private', str(summary))

    async def test_site_issue_write_failure_blocks_without_retry(self):
        self.prepare_two_sites()
        self.engine.scrape_site.side_effect = [RuntimeError('private remote error'), self.many_items(2)]
        with patch('services.news_site_collector._record_site_outcome',
                   side_effect=sqlite3.OperationalError('private DB failure')) as save:
            summary = await self.collect()
        self.assertEqual((summary['state'], summary['stop_reason']), ('waiting', 'site_save_failed'))
        self.assertEqual(self.engine.scrape_site.await_count, 1)
        save.assert_called_once()

    async def test_browser_start_failure_remains_blocking(self):
        self.engine.start.side_effect = RuntimeError('private browser startup')
        summary = await self.collect()
        self.assertEqual((summary['state'], summary['stop_reason']), ('waiting', 'site_collection_failed'))
        self.engine.scrape_site.assert_not_awaited()
        self.assertNotIn('private', str(summary))

    async def test_shutdown_flushes_current_batch_without_marking_site_complete(self):
        self.engine.scrape_site.return_value = self.many_items(43)
        count = 0
        async def fetch(*_):
            nonlocal count
            count += 1
            if count == 23:
                app_lifecycle.shutdown_requested = True
            return {'text': '본문'}
        summary = await self.collect(fetch)
        self.assertEqual((summary['state'], summary['registered'], summary['sites']), ('shutdown', 23, 0))
        app_lifecycle.shutdown_requested = False
        summary = await self.collect()
        self.assertEqual((summary['registered'], summary['saved_total']), (20, 43))

    async def test_cancel_during_lock_acquisition_releases_late_handle(self):
        import threading
        started, finish = threading.Event(), threading.Event()
        handle = object()
        def acquire():
            started.set()
            finish.wait(timeout=5)
            return handle
        self.acquire.side_effect = acquire
        task = asyncio.create_task(self.collect())
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.assert_called_once_with(handle)
        self.engine.start.assert_not_awaited()

    async def test_duplicate_title_link_is_fetched_only_once(self):
        self.engine.scrape_site.return_value = {'state':'finished','items':[
            {'title':'지원','link':'https://example.invalid'},
            {'title':'지원','link':'https://example.invalid'}]}
        fetcher = AsyncMock(return_value={'text':'본문'})
        summary = await self.collect(fetcher)
        self.assertEqual(summary['registered'], 1)
        self.assertEqual(fetcher.await_count, 1)

    async def test_close_failure_still_releases_process_lock(self):
        self.engine.close.side_effect = RuntimeError('close failed')
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await self.collect()
        self.release.assert_called_once()

    async def test_local_reads_are_bounded_at_two_and_do_not_call_google(self):
        self.engine.scrape_site.return_value = self.many_items(8)
        active = peak = 0
        async def fetch(*_):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                await asyncio.sleep(.005)
                return {'text': '지원 대상과 신청 기간을 담은 본문'}
            finally:
                active -= 1
        with patch('ai_classifier.get_client', side_effect=AssertionError('No Google during collection')):
            result = await self.collect(fetch, defer_analysis=True)
        self.assertEqual((peak, active, result['registered']), (2, 0, 8))
        self.assertFalse(self.engine.scrape_site.call_args.args[0]['fallback_enrich_titles'])

    async def test_public_browser_recovers_failed_http_without_provider_call(self):
        import json
        from services.page_coverage import make
        self.engine.fetch_rendered_page = AsyncMock(return_value={
            'text': '브라우저에서 읽은 지원 대상과 신청 기간', 'source_page': make()})
        with patch('ai_classifier.get_client', side_effect=AssertionError('No external vision')):
            result = await self.collect(AsyncMock(return_value={
                'text': '', 'source_page': make('fetch_failed')}), defer_analysis=True)
        self.assertEqual((result['registered'], result['content_failed'], result['browser_recovered']), (2, 0, 2))
        for row in self.conn.execute('SELECT payload_json FROM news_run_candidates'):
            self.assertIn('브라우저에서', json.loads(row[0])['content'])

    async def test_browser_failures_are_circuit_broken_but_titles_retained(self):
        self.engine.scrape_site.return_value = self.many_items(12)
        self.engine.fetch_rendered_page = AsyncMock(return_value=None)
        result = await self.collect(AsyncMock(return_value={'text': ''}))
        self.assertEqual(self.engine.fetch_rendered_page.await_count, 3)
        self.assertEqual((result['registered'], result['content_failed']), (12, 12))

    async def test_rate_limit_login_not_found_and_image_ocr_do_not_retry_browser(self):
        self.engine.fetch_rendered_page = AsyncMock()
        from services.news_site_collector import _needs_browser
        for kind in ('rate_limited', 'unauthorized', 'not_found', 'unsafe_url'):
            self.assertFalse(_needs_browser({'text': '', 'fetch_failure_kind': kind}))
        self.assertFalse(_needs_browser({'text': '', 'images': ['https://example.invalid/poster.png']}))
        result = await self.collect(AsyncMock(return_value={'text': '', 'fetch_failure_kind': 'rate_limited'}))
        self.engine.fetch_rendered_page.assert_not_awaited()
        self.assertEqual(result['content_failed'], 2)

    async def test_attachment_exception_keeps_successful_body_and_review_notice(self):
        import json
        from services.page_coverage import make
        with patch('services.news_site_collector.news_attachments.enrich_page_locally',
                   side_effect=RuntimeError('private attachment error')):
            result = await self.collect(AsyncMock(return_value={
                'text': '원문에서 확보한 지원 내용', 'source_page': make()}))
        self.assertEqual((result['content_failed'], result['registered']), (0, 2))
        for row in self.conn.execute('SELECT payload_json FROM news_run_candidates'):
            value = json.loads(row[0])
            self.assertIn('원문에서 확보한', value['content'])
            self.assertIn('attachment_review', value['source_page']['reasons'])
            self.assertNotIn('private attachment', row[0])

    async def test_cancel_parallel_reads_drains_completed_and_stops_unfinished(self):
        self.engine.scrape_site.return_value = self.many_items(6)
        second_started = asyncio.Event()
        cancelled = asyncio.Event()
        async def fetch(_session, url):
            if url.endswith('/0'):
                return {'text': '이미 완료된 본문'}
            second_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = asyncio.create_task(self.collect(fetch))
        await second_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 1)
        self.engine.close.assert_awaited_once()

    async def test_cancel_while_second_reader_waits_for_browser_lock(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def render(url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.engine.fetch_rendered_page = AsyncMock(side_effect=render)
        task = asyncio.create_task(self.collect(AsyncMock(return_value={'text': ''})))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.engine.fetch_rendered_page.await_count, 1)
        self.engine.close.assert_awaited_once()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 0)

    async def test_cancel_during_flush_keeps_both_concurrently_completed_reads(self):
        started, finish = asyncio.Event(), asyncio.Event()
        writes = 0
        async def db(fn, *args, **kwargs):
            nonlocal writes
            if fn.__name__ == 'register_candidates':
                writes += 1
                if writes == 1:
                    started.set()
                    await finish.wait()
            return fn(self.conn, *args, **kwargs)
        with patch('services.news_site_collector._SAVE_BATCH_SIZE', 1):
            task = asyncio.create_task(collect_registered_sites(self.run,
                analysis_model='fixed', call_db=db, engine_factory=lambda **kw: self.engine,
                content_fetcher=AsyncMock(return_value={'text': '완료된 본문'})))
            await started.wait()
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 2)
        self.release.assert_called_once()

    async def test_http_access_status_classification_does_not_launch_browser(self):
        import aiohttp
        from types import SimpleNamespace
        from unittest.mock import MagicMock, Mock
        from ai_classifier import fetch_page_content_async
        pages = []
        for status, kind in ((401, 'unauthorized'), (404, 'not_found'),
                             (410, 'not_found'), (429, 'rate_limited')):
            response = MagicMock()
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            response.raise_for_status.side_effect = aiohttp.ClientResponseError(
                SimpleNamespace(real_url='https://example.invalid'), (), status=status)
            page = await fetch_page_content_async(Mock(get=Mock(return_value=response)),
                                                 'https://example.invalid')
            self.assertEqual(page['fetch_failure_kind'], kind)
            pages.append(page)
        self.engine.scrape_site.return_value = self.many_items(4)
        self.engine.fetch_rendered_page = AsyncMock()
        result = await self.collect(AsyncMock(side_effect=pages))
        self.assertEqual((result['registered'], result['content_failed']), (4, 4))
        self.engine.fetch_rendered_page.assert_not_awaited()

    async def test_failed_inline_poster_uses_local_capture_once_and_preserves_notice(self):
        import json
        from services.page_coverage import make, with_methods
        image = 'https://example.invalid/poster.png'
        page = dict(text='', images=[image], source_page=make('no_text'))
        enriched = dict(page, attachment_text='', attachment_notices=['첨부파일 다운로드 실패'],
            attachment_reports=[dict(url=image, kind='image', status='download_failed')])
        self.engine.fetch_rendered_page = AsyncMock(return_value=dict(
            text='화면 포스터에서 읽은 지원 대상 및 신청 기간',
            source_page=with_methods(make('rendered_capture_partial'), 'local_ocr')))
        with patch('services.news_site_collector.news_attachments.enrich_page_locally',
                   new=AsyncMock(return_value=enriched)) as enrich:
            result = await self.collect(AsyncMock(return_value=page))
        self.assertEqual((result['registered'], result['content_failed'], result['browser_recovered']), (2, 0, 2))
        self.assertEqual(enrich.await_count, 2)  # One enrichment per post, no duplicate download.
        self.assertTrue(all(call.kwargs == {'capture_images': True}
                            for call in self.engine.fetch_rendered_page.call_args_list))
        for row in self.conn.execute('SELECT payload_json FROM news_run_candidates'):
            value = json.loads(row[0])
            self.assertIn('포스터에서 읽은', value['content'])
            self.assertIn('rendered_capture_partial', value['source_page']['reasons'])
            self.assertIn('attachment_review', value['source_page']['reasons'])
            self.assertIn('local_ocr', value['source_page']['methods'])

    def test_successful_or_skipped_attachment_does_not_trigger_capture(self):
        from services.news_site_collector import _needs_poster_capture
        for status in ('ocr_text_extracted', 'text_extracted', 'skipped_limit', 'read_failed', 'ocr_required'):
            page = dict(text='', images=['https://example.invalid/p.png'], attachment_reports=[
                dict(url='https://example.invalid/p.png', kind='image', status=status)])
            self.assertFalse(_needs_poster_capture(page))

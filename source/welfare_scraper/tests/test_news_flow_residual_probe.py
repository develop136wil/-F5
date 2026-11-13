"""Regressions for collection identity, date context and resumable retention.

These assertions enforce corrected behavior with synthetic, disposable data.
Run only via scripts/run_isolated_tests.py. No real browser, provider, or HTTP.
"""
import json
import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch

from services import app_lifecycle, news_publication as publication
from services import news_analysis_store as jobs, news_result_import as imports
from services import news_unified_feed as feed
from tests import test_news_publication as collection_fixture
from tests import test_news_result_import as import_fixture
from tests import test_news_complete_analysis as complete_fixture


class CollectionResidualProbe(unittest.IsolatedAsyncioTestCase):
    setUp = collection_fixture.PublicationCollectionTests.setUp
    tearDown = collection_fixture.PublicationCollectionTests.tearDown
    db = collection_fixture.PublicationCollectionTests.db
    collect = collection_fixture.PublicationCollectionTests.collect
    row = collection_fixture.PublicationCollectionTests.row
    candidates = collection_fixture.PublicationCollectionTests.candidates

    async def test_blank_line_after_form_heading_does_not_exclude_current_post(self):
        protected_form = '신청서\n작성일: 2020-01-01'
        ambiguous_form = '신청서\n\n작성일: 2020-01-01'
        self.assertIsNone(publication.from_labelled_text(protected_form))
        self.assertIsNone(publication.from_labelled_text(ambiguous_form))
        source = '지원 모집 공고\n' + ('현재 사업의 신청 방법을 확인해주세요.\n' * 5) + ambiguous_form
        fetch = AsyncMock(return_value={'text': source})
        with patch('services.news_attachments.enrich_page_locally', new=AsyncMock(side_effect=lambda _session, page: page)) as enrich:
            summary = await self.collect([self.row(1)], fetch)
        candidate, route, job = self.candidates()[0]
        self.assertEqual(summary['state'], 'finished')
        self.assertEqual(summary.get('outside_publication_period', 0), 0)
        self.assertIsNone(candidate['published_date'])
        self.assertFalse(candidate.get('selection_reason'))
        self.assertEqual(route, 'matched')
        self.assertIsNotNone(job)
        enrich.assert_awaited_once()

    def test_form_whitespace_and_standalone_fields_do_not_mask_real_publication(self):
        for gap in ('\n', '\n\n', '\n \n\t\n'):
            for heading, label in (('신청서', '작성일'), ('참가 신청서:', '작성일'), ('교육', '등록일')):
                for value in (': 2020-01-01', '\n2020-01-01'):
                    with self.subTest(gap=gap, heading=heading, value=value):
                        form = heading + gap + label + value
                        self.assertIsNone(publication.from_labelled_text(form))
                        self.assertEqual(publication.from_labelled_text(
                            '게시일: 2026-09-17\n' + form), '2026-09-17')
        self.assertEqual(publication.from_labelled_text('공지사항\n\n작성일: 2026-09-17'), '2026-09-17')

    async def test_renamed_title_reuses_same_url_receipt_after_shutdown_resume(self):
        source = '지원 사업 본문\n' + '개인이 지원 신청하는 방법을 확인해주세요.\n' * 5
        async def stopping_fetch(*_args):
            app_lifecycle.shutdown_requested = True
            return {'text': source}
        with patch('services.news_attachments.enrich_page_locally', new=AsyncMock(side_effect=lambda _session, page: page)):
            first = await self.collect([self.row(1)], AsyncMock(side_effect=stopping_fetch))
            self.assertEqual(first['state'], 'shutdown')
            self.assertEqual(first['saved_total'], 1)
            app_lifecycle.shutdown_requested = False
            refetch = AsyncMock(return_value={'text': source})
            renamed = self.row(1) | {'title': '지원 모집 1 (제목 정정)'}
            resumed = await self.collect([renamed], refetch)
        self.assertEqual(resumed['state'], 'finished')
        self.assertEqual(resumed['saved_total'], 1)
        self.assertEqual(resumed['registered'], 0)
        refetch.assert_not_awaited()
        captured = self.candidates()
        self.assertEqual(len({candidate['link'] for candidate, _route, _job in captured}), 1)
        self.assertEqual(len({job for _candidate, _route, job in captured}), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM news_analysis_jobs').fetchone()[0], 1)
        self.assertEqual(captured[0][0]['title'], self.row(1)['title'])

    async def test_same_page_anchor_duplicates_are_read_once_but_new_run_reads_edits(self):
        from services import news_profile_store as profiles
        fetch = AsyncMock(return_value={'text': '개인이 신청하는 지원 사업입니다.'})
        with patch('services.news_attachments.enrich_page_locally', new=AsyncMock(side_effect=lambda _session, page: page)):
            await self.collect([self.row(1), self.row(1) | {'title':'지원 제목 수정', 'link':self.row(1)['link'] + '#details'}], fetch)
            fetch.assert_awaited_once()
            self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1, sites=self.sites)
            await self.collect([self.row(1) | {'title':'지원 제목 수정'}], fetch)
            self.assertEqual(fetch.await_count, 2)


class FragmentRouteResidualProbe(unittest.TestCase):
    setUp = import_fixture.TestNewsResultImport.setUp
    tearDown = import_fixture.TestNewsResultImport.tearDown
    new_job = import_fixture.TestNewsResultImport.new_job

    def test_distinct_spa_hash_articles_remain_separate_in_feed_and_import_destination(self):
        import database
        from scraper_engine import _filter_article_links
        links = ['https://example.invalid/board/#/notice/101',
                 'https://example.invalid/board/#/notice/102']
        raw = [dict(title=f'지원 모집 {index}', link=link, _article_context=True)
               for index, link in enumerate(links, 1)]
        self.assertEqual(len(_filter_article_links(raw,
            {'url': 'https://example.invalid/board/', 'news_article_only': True})), 2)
        first = self.new_job(payload=self.payload | {'title': '지원 모집 1', 'link': links[0]})
        second = self.new_job(payload=self.payload | {'title': '지원 모집 2', 'link': links[1]})
        with database.get_connection() as conn:
            ready = feed.read_feed(conn, 'p')['items']
            scoped = [row for row in ready if row['job_id'] in (first, second)]
            self.assertEqual({row['job_id'] for row in scoped}, {first, second})
            before = imports.preview(conn, 'p', first)
            self.assertEqual(before['status'], 'ready')
            saved = imports.import_result(conn, 'p', first, fingerprint=before['fingerprint'], confirmed=True)
            after = imports.preview(conn, 'p', second)
            self.assertEqual(after['status'], 'ready')
            newer = feed.read_feed(conn, 'p')['items']
            self.assertTrue(any(row['job_id'] == second for row in newer))
            second_saved = imports.import_result(conn, 'p', second, fingerprint=after['fingerprint'], confirmed=True)
            self.assertNotEqual(saved['item_id'], second_saved['item_id'])
            self.assertEqual(conn.execute('SELECT link FROM welfare_items WHERE id=?',
                                         (saved['item_id'],)).fetchone()[0], links[0])

    def test_hash_routes_preserve_path_query_but_ordinary_document_anchors_stay_deduplicated(self):
        from services.news_legacy_recovery import identity_url
        base = 'https://example.invalid/board/'
        self.assertEqual(identity_url(base + '#section'), identity_url(base))
        for prefix in ('#/', '#!/'):
            first, second = (base + prefix + 'notice/' + value for value in ('101', '102'))
            self.assertNotEqual(identity_url(first), identity_url(second))
            self.assertNotEqual(identity_url(first + '?tab=notice'), identity_url(first + '?tab=application'))
            self.assertEqual(identity_url(first), first)

    def test_root_hash_articles_require_post_context_and_keep_host_navigation_guards(self):
        from scraper_engine import _filter_article_links
        config = dict(url='https://example.invalid/', news_article_only=True)
        first = dict(title='지원 모집 공고', link='https://example.invalid/#/notice/101', _article_context=True)
        second = first | {'link':'https://example.invalid/#!/notice/102'}
        rejected = [first | {'link':'https://partner.invalid/#/notice/103'},
                    first | {'_in_navigation':True}, first | {'_article_context':False},
                    first | {'link':'https://example.invalid/#section'},
                    first | {'link':'https://example.invalid/#/'}]
        result = _filter_article_links([first, second, *rejected], config)
        self.assertEqual([item['link'] for item in result], [first['link'], second['link']])
        for candidate in rejected:
            with self.subTest(candidate=candidate):
                self.assertEqual(_filter_article_links([candidate], config), [])


class RetentionResumeResidualProbe(unittest.IsolatedAsyncioTestCase):
    setUp = complete_fixture.CompleteNewsInput.setUp
    enqueue = complete_fixture.CompleteNewsInput.enqueue
    response = complete_fixture.CompleteNewsInput.response
    source = complete_fixture.CompleteNewsInput.source

    def assert_waiting_checkpoint_preserved(self, days, *, failed=False):
        from services import news_record_retention as retention, news_profile_workflow as workflow
        from services.news_analysis_context import NewsAnalysisContext
        from services.news_complete_analysis import _checkpoint, _identity
        self.conn.execute('PRAGMA foreign_keys=ON')
        source = self.source(3000)
        job = self.enqueue(source)
        stored = jobs.get_job(self.conn, job)
        profile = NewsAnalysisContext.from_run(self.conn, stored['run_id']).profile
        identity = _identity(profile, json.loads(stored['input_json']), stored['model'])
        checkpoint = _checkpoint({'identity': identity,
            'done': [{'span': [0, 600], 'result': self.response()}],
            'pending': [[600, len(source)]]}, identity, source)
        claim = jobs.claim(self.conn, job, now=0)
        jobs.save_checkpoint(self.conn, job, claim, checkpoint)
        jobs.defer(self.conn, job, claim, retry_after=60, reason='rate_limited', failed=failed)
        workflow_id = workflow.create_workflow(self.conn, stored['run_id'])
        now = dt.datetime(2026, 9, 18, 12, tzinfo=dt.timezone.utc)
        old = (now - dt.timedelta(days=40)).isoformat()
        self.conn.execute('UPDATE news_run_snapshots SET created_at=? WHERE run_id=?', (old, stored['run_id']))
        self.conn.execute('UPDATE news_job_events SET created_at=? WHERE job_id=?', (old, job))
        self.conn.execute('''UPDATE news_profile_workflows SET state='waiting',stage='analyzing',
            summary_json=?,updated_at=? WHERE workflow_id=?''',
            (json.dumps({'collection': {'state': 'finished'}}), old, workflow_id))
        if failed:
            self.conn.execute("UPDATE news_profile_workflows SET state='failed',error_code='analysis_failed' WHERE workflow_id=?", (workflow_id,))
        self.conn.commit()
        self.assertTrue(workflow.get_workflow(self.conn, workflow_id)['can_resume'])
        self.assertEqual(len(json.loads(jobs.get_job(self.conn, job)['checkpoint_json'])['done']), 1)
        preview = retention.preview(self.conn, days, now=now)
        self.assertEqual(preview['deleted_jobs'], 0)
        self.assertEqual(preview['deleted_runs'], 0)
        self.assertEqual(preview['skipped_active'], 1)
        result = retention.cleanup(self.conn, days, now=now)
        self.assertEqual(result['deleted_jobs'], 0)
        self.assertEqual(len(json.loads(jobs.get_job(self.conn, job)['checkpoint_json'])['done']), 1)
        self.assertTrue(workflow.get_workflow(self.conn, workflow_id)['can_resume'])
        # A separately confirmed terminal cancellation makes the old checkpoint
        # eligible again; retention never silently cancels work itself.
        self.conn.execute("UPDATE news_profile_workflows SET state='cancelled',stage='cancelled' WHERE workflow_id=?", (workflow_id,))
        self.conn.commit()
        self.assertEqual(retention.cleanup(self.conn, days, now=now)['deleted_jobs'], 1)

    async def test_seven_day_opt_in_cleanup_preserves_resumable_part_checkpoint(self):
        self.assert_waiting_checkpoint_preserved(7)

    async def test_thirty_day_opt_in_cleanup_preserves_resumable_part_checkpoint(self):
        self.assert_waiting_checkpoint_preserved(30)

    async def test_recoverable_failure_also_preserves_part_checkpoint(self):
        self.assert_waiting_checkpoint_preserved(30, failed=True)

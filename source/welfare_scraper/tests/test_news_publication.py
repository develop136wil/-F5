"""Offline selection contracts: dates are original publication dates, not deadlines."""
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock, patch

from services import news_publication as publication, news_profile_store as store
from services import news_analysis_store as jobs, app_lifecycle
from services.news_profile import NewsProfile
from services.news_candidate_intake import register_candidates
from services.news_site_collector import collect_registered_sites


class PublicationTests(unittest.TestCase):
    def test_defaults_and_inclusive_recent_windows(self):
        profile = NewsProfile('p', 1, '조건', '전국')
        self.assertEqual(profile.publication_period, '30')
        for days, start in [('7', '2026-09-11'), ('30', '2026-08-19'), ('90', '2026-06-20')]:
            window = publication.freeze_window(replace(profile, publication_period=days), today=date(2026, 9, 17))
            self.assertEqual(window, dict(start_date=start, end_date='2026-09-17', period=days))
            self.assertFalse(publication.outside_window(start, window))
            self.assertFalse(publication.outside_window('2026-09-17', window))
            self.assertTrue(publication.outside_window('2026-09-18', window))
            self.assertFalse(publication.outside_window(None, window))

    def test_custom_range_validation(self):
        for values in [('custom', '', ''), ('custom', '2026-09-18', '2026-09-17'),
                       ('custom', '2026-02-30', '2026-09-17'), ('all', '', ''),
                       ('30', '2026-2-1', ''), ([], '', '')]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                publication.validate_settings(*values)
        publication.validate_settings('custom', '2026-09-17', '2026-09-17')

    def test_explicit_publication_labels_only(self):
        for text in ('작성일: 2026. 9. 2.', '등록일\n2026-09-02',
                     '게시일 2026년 9월 2일', 'Published: 2026/9/2',
                     '등록일 2026-09-02 마감일 2026-10-05'):
            with self.subTest(text=text):
                self.assertEqual(publication.from_labelled_text(text), '2026-09-02')
        for text in ('마감일 2026-09-02', '신청 기간 2026-09-02 ~ 2026-09-30',
                     '행사일: 2026-09-02', '수집일 2026-09-02', '수정일 2026-09-02',
                     '2026-09-02', '등록일: 미상, 신청일: 2026-09-02'):
            with self.subTest(text=text):
                self.assertIsNone(publication.from_labelled_text(text))
        self.assertIsNone(publication.from_list_item({'date': '2026-09-02'}))

    def test_old_snapshot_is_not_reinterpreted(self):
        self.assertIsNone(publication.window_from_snapshot({'schema_version': 1}))

    def test_non_article_date_labels_are_not_publication_metadata(self):
        for label in ('신청서 작성일', '교육 등록일', '재등록일', '최종 수정 작성일', '수강 등록일'):
            for separator in (': ', ':\n'):
                with self.subTest(label=label, separator=separator):
                    self.assertIsNone(publication.from_labelled_text(label + separator + '2025-01-01'))
        for text in ('신청서\n작성일: 2025-01-01', '교육\n등록일\n2025-01-01',
                     '작성일:\n신청 기간: 2025-01-01', '등록일:\n날짜 미상 2025-01-01'):
            with self.subTest(text=text):
                self.assertIsNone(publication.from_labelled_text(text))

    def test_actual_publication_after_non_article_date_wins_same_or_next_line(self):
        for label in ('신청서 작성일', '교육 등록일', '재등록일'):
            for separator in (' ', ' | ', '\n'):
                for date_separator in (': ', ':\n'):
                    text = label + ': 2025-01-01' + separator + '게시일' + date_separator + '2026-09-17'
                    with self.subTest(text=text):
                        self.assertEqual(publication.from_labelled_text(text), '2026-09-17')

    def test_independent_registration_and_authoring_metadata_still_work(self):
        for text in ('등록일: 2026-09-17', '작성일\n2026-09-17',
                     '조회수 10 | 작성일: 2026-09-17', '• 등록일: 2026-09-17',
                     '게시일: 2026-09-17 | 작성일: 2026-09-17'):
            with self.subTest(text=text):
                self.assertEqual(publication.from_labelled_text(text), '2026-09-17')

    def test_conflicting_publication_metadata_stays_unknown(self):
        for separator in (' ', ' | ', '\n'):
            with self.subTest(separator=separator):
                self.assertIsNone(publication.from_labelled_text(
                    '등록일: 2025-01-01' + separator + '게시일: 2026-09-17'))

    def test_display_metadata_is_never_an_ai_instruction(self):
        original = dict(title='지원 모집', link='https://example.invalid/post/1', content='본문', site_name='기관')
        self.assertEqual(publication.analysis_payload({**original, 'published_date': '2026-09-02',
            'publication_status': 'known', 'selection_reason': ''}), original)
        self.assertIn('untrusted_new_field', publication.analysis_payload({**original, 'untrusted_new_field': 'x'}))


class PublicationCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_lifecycle.shutdown_requested = False
        self.conn = sqlite3.connect(':memory:')
        store.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.profile = NewsProfile('p', 1, '조건', '전국', site_ids=('1',), keywords=('지원',),
            publication_period='custom', publication_start_date='2026-09-01', publication_end_date='2026-09-17')
        store.create_profile(self.conn, self.profile)
        self.sites = [dict(id=1, name='기관', url='https://example.invalid/board', selectors={})]
        self.run = store.prepare_run(self.conn, 'p', expected_revision=1, sites=self.sites)
        self.engine = unittest.mock.MagicMock(start=AsyncMock(), close=AsyncMock(), scrape_site=AsyncMock(),
                                              fetch_rendered_page=AsyncMock(return_value=None))
        self.acquire = patch('services.scraper_service._acquire_scrape_lock', return_value=object()).start()
        self.release = patch('services.scraper_service._release_scrape_lock').start()

    def tearDown(self):
        patch.stopall()
        self.conn.close()
        app_lifecycle.shutdown_requested = False

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def collect(self, items, fetcher=None):
        self.engine.scrape_site.return_value = {'state': 'finished', 'items': items}
        return await collect_registered_sites(self.run, analysis_model='synthetic', call_db=self.db,
            engine_factory=lambda **kwargs: self.engine,
            content_fetcher=fetcher or AsyncMock(return_value={'text': '지원 안내 본문'}))

    def row(self, number, **metadata):
        return dict(title=f'지원 모집 {number}', link=f'https://example.invalid/post/{number}', **metadata)

    def candidates(self):
        return [(json.loads(payload), route, job_id) for payload, route, job_id in self.conn.execute(
            'SELECT payload_json,route,job_id FROM news_run_candidates WHERE run_id=?', (self.run,))]

    async def test_list_outside_period_is_preserved_without_body_or_ai(self):
        fetcher = AsyncMock(return_value={'text': '지원 안내'})
        result = await self.collect([self.row(1, published_date='2026-08-31'),
                                     self.row(2, published_date='2026-09-17')], fetcher)
        self.assertEqual(fetcher.await_count, 1)
        self.assertEqual(result['outside_publication_period'], 1)
        excluded = next(row for row in self.candidates() if row[1] == 'excluded')
        self.assertIsNone(excluded[2])
        self.assertEqual(excluded[0]['selection_reason'], 'outside_publication_period')
        self.assertEqual(excluded[0]['published_date'], '2026-08-31')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 1)

    async def test_body_date_filters_before_attachment_ocr(self):
        fetcher = AsyncMock(return_value={'text': '등록일: 2026-08-01\n지원 내용',
                                         'attachments': ['https://example.invalid/a.pdf']})
        with patch('services.news_site_collector.news_attachments.enrich_page_locally', new_callable=AsyncMock) as enrich:
            await self.collect([self.row(1)], fetcher)
        enrich.assert_not_awaited()
        self.assertEqual(self.candidates()[0][1], 'excluded')

    async def test_deadline_and_unknown_dates_keep_candidates(self):
        await self.collect([self.row(1, date='2025-01-01')],
                           AsyncMock(return_value={'text': '마감일: 2025-01-01\n지원 내용'}))
        payload, route, job_id = self.candidates()[0]
        self.assertEqual(payload['publication_status'], 'unknown')
        self.assertIsNone(payload['published_date'])
        self.assertEqual(route, 'matched')
        self.assertIsNotNone(job_id)
        self.assertNotIn('publication_status', json.loads(jobs.get_job(self.conn, job_id)['input_json']))

    async def test_form_and_course_dates_do_not_exclude_readable_candidates(self):
        bodies = ['신청서 작성일: 2025-01-01\n지원 내용',
                  '교육 등록일: 2025-01-01 | 게시일: 2026-09-17\n지원 내용',
                  '재등록일: 2025-01-01\n게시일\n2026-09-17\n지원 내용']
        with patch('services.news_site_collector.news_attachments.enrich_page_locally',
                   new=AsyncMock(side_effect=lambda _session, page: page)) as enrich:
            result = await self.collect([self.row(index) for index in range(3)],
                AsyncMock(side_effect=[{'text': text} for text in bodies]))
        self.assertEqual(result.get('outside_publication_period', 0), 0)
        self.assertEqual(enrich.await_count, 3)
        rows = self.candidates()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(route == 'matched' and job_id for _, route, job_id in rows))
        self.assertEqual({payload['published_date'] for payload, _, _ in rows}, {None, '2026-09-17'})

    async def test_direct_and_semantic_queues_strip_only_display_metadata(self):
        from services.news_direct_analysis import queue_candidates
        from services.news_semantic_gate import _finish
        candidates = [publication.attach(dict(title='일반 공고', link='https://example.invalid/post/90', content='본문'), None),
                      publication.attach(dict(title='별도 공고', link='https://example.invalid/post/91', content='본문'), '2026-09-02')]
        receipts = register_candidates(self.conn, self.run, '1', candidates, model='synthetic')
        self.assertEqual(queue_candidates(self.conn, self.run, 'synthetic', 1), 1)
        result = _finish(self.conn, self.run, receipts[1]['candidate_key'], score=1.0, threshold=0.5,
                         analysis_model='synthetic', scoring_version='synthetic')
        self.assertEqual(result['state'], 'semantic_matched')
        for row in self.conn.execute('SELECT input_json FROM news_analysis_jobs'):
            self.assertNotIn('publication_status', json.loads(row[0]))
        self.assertTrue(all('publication_status' in row[0] for row in self.candidates()))

    async def test_excluded_title_is_preserved_without_reading_body_or_ocr(self):
        profile = replace(self.profile, revision=2, excluded_keywords=('채용',))
        store.save_revision(self.conn, profile, expected_revision=1)
        self.run = store.prepare_run(self.conn, 'p', expected_revision=2, sites=self.sites)
        fetcher = AsyncMock(side_effect=AssertionError('Excluded title must not fetch body'))
        result = await self.collect([dict(self.row(1), title='지원기관 직원 채용 공고')], fetcher)
        fetcher.assert_not_awaited()
        self.assertEqual(result['excluded_by_title'], 1)
        self.assertEqual(self.candidates()[0][1], 'excluded')

    async def test_new_posts_prioritized_without_skipping_changed_known_posts(self):
        register_candidates(self.conn, self.run, '1', [dict(self.row(1), content='이전 본문')], model='synthetic')
        self.run = store.prepare_run(self.conn, 'p', expected_revision=1, sites=self.sites)
        fetcher = AsyncMock(return_value={'text': '변경된 지원 본문'})
        await self.collect([self.row(1), self.row(2)], fetcher)
        self.assertEqual([call.args[1] for call in fetcher.await_args_list],
                         ['https://example.invalid/post/2', 'https://example.invalid/post/1'])
        self.assertEqual(len(self.candidates()), 2)

    async def test_resume_uses_same_window_and_setting_change_preserves_old_rows(self):
        original = store.get_run_snapshot(self.conn, self.run)['publication_window']
        await self.collect([self.row(1, published_date='2026-09-02')])
        store.save_revision(self.conn, replace(self.profile, revision=2, publication_period='7'), expected_revision=1)
        with patch('services.news_publication.freeze_window', side_effect=AssertionError('No current date on resume')):
            self.assertEqual(store.get_run_snapshot(self.conn, self.run)['publication_window'], original)
            result = await self.collect([self.row(1, published_date='2026-09-02')])
        self.assertEqual(result['registered'], 0)
        self.assertEqual(len(self.candidates()), 1)

    async def test_old_run_without_window_keeps_earlier_scope(self):
        captured = store.get_run_snapshot(self.conn, self.run)
        captured.pop('publication_window')
        # Simulate the immutable v3.0.3 input, not a mutable current preference.
        for key in ('publication_period', 'publication_start_date', 'publication_end_date'):
            captured['profile'].pop(key)
        profile_json = json.dumps(captured['profile'], ensure_ascii=False)
        import hashlib
        profile_digest = hashlib.sha256(json.dumps(captured['profile'], sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.conn.execute('UPDATE news_profile_versions SET snapshot_json=?,fingerprint=? WHERE profile_id=?',
                          (profile_json, profile_digest, 'p'))
        encoded = store._run_json(captured)
        self.conn.execute('UPDATE news_run_snapshots SET snapshot_json=?,fingerprint=? WHERE run_id=?',
                          (encoded, store._run_digest(encoded), self.run))
        await self.collect([self.row(1, published_date='2025-01-01')])
        self.assertEqual(self.candidates()[0][1], 'matched')

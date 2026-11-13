"""Persisted production pagination, using only caller-owned SQLite and fake pages."""
import asyncio
import sqlite3
import unittest
from unittest.mock import MagicMock

from services import app_lifecycle
from services.news_profile import NewsProfile
from services import news_profile_store as profiles
from services import news_analysis_store as jobs
from services import news_official_results as official
from services.news_bokjiro_collector import collect_bokjiro_run, _progress


def provider_for(pages):
    async def stream(**kwargs):
        for page in pages:
            if isinstance(page, BaseException):
                raise page
            yield page
    return MagicMock(side_effect=stream)


class TestNewsBokjiroCollector(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_lifecycle.shutdown_requested = False
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn); jobs.ensure_schema(self.conn); official.ensure_schema(self.conn)
        profile = NewsProfile('p', 1, '노년 주거', '전국', sources=('bokjiro',), site_ids=(),
            life_cycles=('노년', '기관 자체 생애주기'), characteristics=('독거',),
            interest_topics=('주거', '기관 자체 분야'), bokjiro_mode='official_tags_v1')
        profiles.create_profile(self.conn, profile)
        self.run_id = profiles.prepare_run(self.conn, 'p', expected_revision=1, sites=[])
        self.item = {'service_id': 'svc-1', 'title': '노년 주거 지원', 'summary': '주거비 지원',
            'provider': '보건복지부', 'link': 'https://untrusted.invalid/redirect',
            'life_cycles': ['노년'], 'household_situations': [], 'interest_topics': ['주거']}

    def tearDown(self):
        self.conn.close(); app_lifecycle.shutdown_requested = False

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    def page(self, number, *, count=9, complete=False):
        items = [] if complete else [self.item | {'service_id': f'svc-{number}-{i}'} for i in range(count)]
        return dict(state='page', page=number, items=items, scanned=len(items), total_reported=(number-1)*9 if complete else 81,
            complete=complete, fingerprint='' if complete else f'page-{number}')

    async def test_collects_official_filters_without_ai_and_ignores_redirect(self):
        provider = provider_for([self.page(1), self.page(2, complete=True)])
        result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider)
        self.assertEqual((result['registered'], result['new'], result['ai_called']), (9, 9, False))
        self.assertTrue(result['complete'])
        provider.assert_called_once_with(life_cycles=('노년',), characteristics=(),
            interest_topics=('주거',), start_page=1, max_pages=5)
        payload = self.conn.execute('SELECT payload_json FROM news_official_results').fetchone()[0]
        self.assertIn('www.bokjiro.go.kr', payload)
        self.assertNotIn('untrusted.invalid', payload)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    async def test_more_than_30_and_later_than_five_pages_are_not_lost(self):
        first = await collect_bokjiro_run(self.run_id, call_db=self.db,
            provider=provider_for([self.page(i) for i in range(1, 6)]))
        self.assertEqual((first['state'], first['registered'], first['next_page']), ('more_pages', 45, 6))
        second_provider = provider_for([self.page(6), self.page(7, complete=True)])
        second = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=second_provider)
        self.assertEqual((second['state'], second['registered']), ('finished', 54))
        self.assertEqual(second_provider.call_args.kwargs['start_page'], 6)
        final_provider = provider_for([])
        final = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=final_provider)
        final_provider.assert_not_called()
        self.assertEqual(final, {key: value for key, value in second.items() if key != 'pages_processed'})

    async def test_failed_page_remains_resumable_after_persisted_success(self):
        first = await collect_bokjiro_run(self.run_id, call_db=self.db,
            provider=provider_for([self.page(1), RuntimeError('private provider error')]))
        self.assertEqual((first['state'], first['registered'], first['next_page']), ('waiting', 9, 2))
        self.assertEqual(first['stop_reason'], 'bokjiro_page_failed')
        self.assertNotIn('private', str(first))
        self.assertFalse(first['complete'])
        second_provider = provider_for([self.page(2), self.page(3, complete=True)])
        result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=second_provider)
        self.assertEqual(second_provider.call_args.kwargs['start_page'], 2)
        self.assertEqual((result['registered'], result['new'], result['pages_failed']), (18, 18, 1))

    async def test_cancelled_read_keeps_committed_page_cursor(self):
        with self.assertRaises(asyncio.CancelledError):
            await collect_bokjiro_run(self.run_id, call_db=self.db,
                provider=provider_for([self.page(1), asyncio.CancelledError()]))
        self.assertEqual(_progress(self.conn, self.run_id)['next_page'], 2)
        self.assertEqual(_progress(self.conn, self.run_id)['registered'], 9)

    async def test_cancelled_page_write_drains_before_releasing_cursor(self):
        from services.news_bokjiro_collector import _save_page
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_db(fn, *args, **kwargs):
            if fn is _save_page:
                entered.set()
                await release.wait()
            return fn(self.conn, *args, **kwargs)
        task = asyncio.create_task(collect_bokjiro_run(self.run_id, call_db=delayed_db,
            provider=provider_for([self.page(1)])))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((_progress(self.conn, self.run_id)['next_page'],
                          _progress(self.conn, self.run_id)['registered']), (2, 9))

    async def test_empty_page_before_reported_total_is_waiting(self):
        result = await collect_bokjiro_run(self.run_id, call_db=self.db,
            provider=provider_for([self.page(1), self.page(2, complete=True) | {'total_reported': 81}]))
        self.assertEqual((result['state'], result['next_page'], result['registered']), ('waiting', 2, 9))

    async def test_progress_survives_new_connection(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.db'
            first = sqlite3.connect(path)
            self.conn.backup(first); self.conn.close(); self.conn = first
            await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider_for([self.page(1)]))
            self.conn.close(); self.conn = sqlite3.connect(path)
            provider = provider_for([self.page(2, complete=True)])
            result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider)
            self.assertEqual(provider.call_args.kwargs['start_page'], 2)
            self.assertEqual(result['registered'], 9)
            self.conn.close(); self.conn = sqlite3.connect(':memory:')

    async def test_repeated_page_or_invalid_item_cannot_advance_cursor(self):
        await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider_for([self.page(1)]))
        for page in (self.page(2) | {'fingerprint': 'page-1'},
                     self.page(2) | {'items': [self.item | {'title': None}]}):
            result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider_for([page]))
            self.assertEqual((result['state'], result['next_page'], result['registered']), ('waiting', 2, 9))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_bokjiro_pages').fetchone()[0], 1)

    async def test_busy_shutdown_and_invalid_response_do_not_report_complete(self):
        for response in ({'state': 'busy'}, {'state': 'shutdown'}, {}):
            result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider_for([response]))
            self.assertFalse(result['complete'])
            self.assertEqual(result['registered'], 0)
        app_lifecycle.shutdown_requested = True
        provider = provider_for([])
        self.assertEqual((await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider))['state'], 'shutdown')
        provider.assert_not_called()

    async def test_empty_provider_is_waiting_not_finished(self):
        result = await collect_bokjiro_run(self.run_id, call_db=self.db, provider=provider_for([]))
        self.assertEqual((result['state'], result['registered']), ('waiting', 0))


if __name__ == '__main__':
    unittest.main()

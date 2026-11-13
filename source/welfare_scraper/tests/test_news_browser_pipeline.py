"""Opt-in headless browser/local HTTP fixture; use run_isolated_tests.py only."""
import asyncio
from contextlib import AsyncExitStack
from collections import Counter
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('WELFARE_BROWSER_PIPELINE') == '1', 'opt-in browser fixture')
class BrowserPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_later_page_failure_preserves_candidates_and_resume_fills_gap(self):
        from scraper_engine import ScraperEngine
        from services import news_profile_store as profiles, news_analysis_store as jobs
        from services.news_profile import NewsProfile
        from services.news_site_collector import collect_registered_sites

        self.assertEqual(os.environ.get('PYTHON_DOTENV_DISABLED'), '1')
        self.assertFalse(os.environ.get('GOOGLE_API_KEY'))
        origin = 'https://pagination.example.invalid'
        failing = True
        pages = Counter()
        bodies = Counter()
        blocked = []
        outcomes = []

        def board(number):
            # Adjacent pages overlap by one item, like a pinned/repeated post.
            first = (number - 1) * 9
            rows = ''.join(f'<tr><td><a href="/notice/{i}">돌봄 지원 {i}</a></td></tr>'
                           for i in range(first, first + 10))
            next_link = f'<a class="next" href="/notice/list.do?pageIndex={number + 1}">다음</a>' if number < 3 else ''
            pager = f'<div class="paginate"><span aria-current="page">{number}</span>{next_link}</div>'
            return f'<html><body><table><tbody>{rows}</tbody></table>{pager}</body></html>'

        class BrowserProxy:
            def __init__(self, raw): self.raw = raw
            async def new_context(self, **kwargs):
                context = await self.raw.new_context(**kwargs)
                async def route(request_route):
                    url = request_route.request.url
                    if url in {origin + f'/notice/list.do?pageIndex={i}' for i in (1, 2, 3)}:
                        number = int(url.rsplit('=', 1)[1])
                        pages[number] += 1
                        # A real HTTP error page has no configured list selector.
                        await request_route.fulfill(
                            status=503 if failing and number == 3 else 200,
                            content_type='text/html; charset=utf-8',
                            body='<html><body>시험용 일시 장애</body></html>'
                                 if failing and number == 3 else board(number))
                    else:
                        blocked.append(url)
                        await request_route.abort()
                await context.route('**/*', route)
                return context
            async def close(self): await self.raw.close()

        class FixtureEngine(ScraperEngine):
            async def start(self):
                await super().start()
                self.browser = BrowserProxy(self.browser)
            async def scrape_site(self, *args, **kwargs):
                result = await super().scrape_site(*args, **kwargs)
                outcomes.append((result['state'], len(result['items'])))
                return result

        async def fetch(session, url):
            self.assertIn(url, {origin + f'/notice/{i}' for i in range(28)})
            bodies[url] += 1
            return {'text': '시험 본문: 돌봄 지원 신청 안내. 중복 지원은 제외합니다.'}

        with tempfile.TemporaryDirectory(prefix='news-pagination-db-') as directory:
            path = Path(directory) / 'fixture.db'
            conn = sqlite3.connect(path)
            try:
                profiles.ensure_schema(conn)
                jobs.ensure_schema(conn)
                profiles.create_profile(conn, NewsProfile('pages', 1, '시험', '전국',
                    site_ids=('1',), keywords=('돌봄',)))
                run = profiles.prepare_run(conn, 'pages', expected_revision=1, sites=[dict(
                    id=1, name='여러 페이지 시험', url=origin + '/notice/list.do?pageIndex=1', max_pages=4,
                    selectors=dict(list_item='tbody tr', title='td a'))])
                async def call_db(fn, *args, **kwargs): return fn(conn, *args, **kwargs)
                async def collect():
                    return await asyncio.wait_for(collect_registered_sites(run,
                        analysis_model='offline-fixture', call_db=call_db,
                        engine_factory=FixtureEngine, content_fetcher=fetch), 90)
                with patch.object(ScraperEngine, '_install_browser', side_effect=AssertionError('No downloads')):
                    failed = await collect()
                    self.assertEqual((failed['state'], failed['site_partial']), ('finished', 1))
                    self.assertEqual(failed['site_issues'], [dict(site_id='1',
                        site_name='여러 페이지 시험', issue_code='site_partial')])
                    self.assertEqual((failed['sites'], failed['registered'], failed['saved_total']), (0, 19, 19))
                    self.assertEqual(outcomes, [('partial', 19)])
                    before = conn.execute('SELECT payload_json FROM news_run_candidates ORDER BY payload_json').fetchall()
                    self.assertEqual(len(before), 19)
                    conn.close()
                    conn = sqlite3.connect(path)
                    self.assertEqual(conn.execute('SELECT payload_json FROM news_run_candidates ORDER BY payload_json').fetchall(), before)
                    failing = False
                    resumed = await collect()
                    self.assertEqual(resumed, dict(state='finished', sites=1, registered=9,
                                                  saved_total=28, content_failed=0))
                    self.assertEqual(outcomes, [('partial', 19), ('finished', 28)])
                    self.assertEqual(pages, Counter({1: 2, 2: 2, 3: 2}))
                    self.assertEqual(bodies, Counter({origin + f'/notice/{i}': 1 for i in range(28)}))
                    after = conn.execute('SELECT payload_json FROM news_run_candidates').fetchall()
                    self.assertTrue(set(before).issubset(set(after)))
                    self.assertEqual(len({json.loads(row[0])['link'] for row in after}), 28)
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertFalse(blocked)
            finally:
                conn.close()

    async def test_cancel_reopen_resume_without_duplicate_candidates(self):
        from aiohttp import web
        from ai_classifier import fetch_page_content_async
        from scraper_engine import ScraperEngine
        from services import news_profile_store as profiles, news_analysis_store as jobs
        from services.news_profile import NewsProfile
        from services.news_site_collector import collect_registered_sites

        self.assertEqual(os.environ.get('PYTHON_DOTENV_DISABLED'), '1')
        self.assertFalse(os.environ.get('GOOGLE_API_KEY'))
        fixture_origin = 'https://board.example.invalid'
        body_requests = Counter()
        blocked = []
        entered = asyncio.Event()
        pause_once = True

        async def body(request):
            index = int(request.match_info['index'])
            body_requests[index] += 1
            return web.Response(text=f'<html><body><article><h1>돌봄 지원 {index}</h1>'
                '<p>시험 본문: 만 65세 이상 주민에게 월 2만원을 지원합니다. '
                '중복 지원은 제외합니다. 주민센터에서 신청하세요.</p></article></body></html>',
                content_type='text/html')

        app = web.Application()
        app.router.add_get('/notice/{index}', body)
        runner = web.AppRunner(app)
        await runner.setup()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        local_origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
        await web.SockSite(runner, listener).start()
        board = '<html><body><table><tbody>' + ''.join(
            f'<tr><td class="title"><a href="/notice/{i}">돌봄 지원 {i}</a></td>'
            '<td class="date">2026-09-09</td></tr>' for i in range(30)) + '</tbody></table></body></html>'

        class BrowserProxy:
            def __init__(self, raw): self.raw = raw
            async def new_context(self, **kwargs):
                context = await self.raw.new_context(**kwargs)
                async def route(request_route):
                    if request_route.request.url == fixture_origin + '/notice/list.do':
                        await request_route.fulfill(status=200, content_type='text/html; charset=utf-8', body=board)
                    else:
                        blocked.append(request_route.request.url)
                        await request_route.abort()
                await context.route('**/*', route)
                return context
            async def close(self): await self.raw.close()

        class FixtureEngine(ScraperEngine):
            async def start(self):
                await super().start()
                self.browser = BrowserProxy(self.browser)

        async def fetch(session, url):
            nonlocal pause_once
            self.assertTrue(url.startswith(fixture_origin + '/notice/'))
            index = int(url.rsplit('/', 1)[1])
            if index == 21 and pause_once:
                pause_once = False
                entered.set()
                await asyncio.Event().wait()
            async def validate(target):
                self.assertEqual(target, local_origin + f'/notice/{index}')
                return target
            return await fetch_page_content_async(session, local_origin + f'/notice/{index}',
                                                   url_validator=validate)

        conn = None
        task = None
        try:
            async with AsyncExitStack() as cleanup:
                directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='news-browser-db-'))
                async def close_before_removing_db():
                    nonlocal conn
                    if task and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if conn:
                        conn.close()
                        conn = None
                cleanup.push_async_callback(close_before_removing_db)
                db_path = Path(directory) / 'fixture.db'
                conn = sqlite3.connect(db_path)
                profiles.ensure_schema(conn)
                jobs.ensure_schema(conn)
                profiles.create_profile(conn, NewsProfile('browser', 1, '시험', '전국',
                    site_ids=('1',), keywords=('돌봄',)))
                run = profiles.prepare_run(conn, 'browser', expected_revision=1, sites=[dict(
                    id=1, name='시험 게시판', url=fixture_origin + '/notice/list.do', max_pages=1,
                    selectors=dict(list_item='table tbody tr', title='td.title a', date='td.date'))])
                async def call_db(fn, *args, **kwargs): return fn(conn, *args, **kwargs)
                async def collect():
                    return await collect_registered_sites(run, analysis_model='offline-fixture',
                        call_db=call_db, engine_factory=FixtureEngine, content_fetcher=fetch)
                with patch.object(ScraperEngine, '_install_browser', side_effect=AssertionError('No downloads')):
                    task = asyncio.create_task(collect())
                    await asyncio.wait_for(entered.wait(), 60)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task
                    saved = conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0]
                    # Two concurrent reads may leave one partner unfinished at
                    # cancellation. Only durable receipts must avoid refetch.
                    self.assertGreaterEqual(saved, 20)
                    self.assertLess(saved, 30)
                    saved_ids = {int(json.loads(row[0])['link'].rsplit('/', 1)[1]) for row in
                        conn.execute('SELECT payload_json FROM news_run_candidates')}
                    fetched_before = dict(body_requests)
                    conn.close()
                    conn = sqlite3.connect(db_path)
                    resumed = await asyncio.wait_for(collect(), 60)
                    self.assertEqual((resumed['registered'], resumed['saved_total']), (30 - saved, 30))
                    self.assertEqual(resumed['content_failed'], 0)
                    repeated = await asyncio.wait_for(collect(), 60)
                    self.assertEqual(repeated['registered'], 0)
                    self.assertEqual(set(body_requests), set(range(30)))
                    self.assertTrue(all(body_requests[i] == fetched_before[i] for i in saved_ids))
                    self.assertTrue(all(1 <= count <= 2 for count in body_requests.values()))
                    payloads = [json.loads(row[0]) for row in conn.execute(
                        'SELECT payload_json FROM news_run_candidates')]
                    self.assertEqual(len({row['link'] for row in payloads}), 30)
                    self.assertTrue(all('중복 지원은 제외' in row['content'] for row in payloads))
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertFalse(blocked)
                conn.close()
                conn = None
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if conn: conn.close()
            await runner.cleanup()

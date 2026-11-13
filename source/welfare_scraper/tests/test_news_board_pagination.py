"""Board scope and bounded pager regressions; only synthetic URLs and HTML."""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

from scraper_engine import ScraperEngine, _filter_article_links
from services.board_detail_links import article_in_board, next_list_page, list_page_in_board
from tests.test_board_detail_links import DBWC, NOTICE, NEWS, PROGRAM


def control(href, label='2', **extra):
    return dict(href=href, label=label, **extra)


class BoardScopeTests(unittest.TestCase):
    def test_list_view_query_boards_and_slug_routes_survive(self):
        pairs = [('https://example.invalid/notice/list.do', 'https://example.invalid/notice/view.do?nttId=1'),
                 ('https://example.invalid/bbs.asp?code=abc', 'https://example.invalid/bbs.asp?code=abc&bmode=D&idx=1'),
                 ('https://example.invalid/bbs/board.php?bo_table=news', 'https://example.invalid/bbs/board.php?bo_table=news&wr_id=1'),
                 ('https://example.invalid/cop/bbs/selectBoardList.do?bbsId=B01', 'https://example.invalid/cop/bbs/selectBoardArticle.do?bbsId=B01&nttId=1'),
                 ('https://example.invalid/news', 'https://example.invalid/news/summer-program'),
                 (DBWC, DBWC + '?bmode=view&bm_id=b02&idx=624'),
                 (NOTICE, NOTICE.replace('list.do', 'view.do') + '&boardId=42'),
                 (NEWS, NEWS.replace('list.do', 'view.do') + '&boardId=42'),
                 (PROGRAM, PROGRAM.replace('list.do', 'view.do') + '&reqstId=42')]
        for source, detail in pairs:
            with self.subTest(source=source):
                self.assertTrue(article_in_board(source, detail))

    def test_different_board_path_query_missing_identity_and_attack_are_rejected(self):
        source = 'https://example.invalid/notice/list.do?bbsId=B01'
        for detail in ('https://example.invalid/jobs/view.do?bbsId=B01&nttId=1',
                       'https://example.invalid/notice/view.do?bbsId=B02&nttId=1',
                       'https://example.invalid/notice/view.do?nttId=1',
                       'https://example.invalid/notice/view.do?bbsId=B01&bbsId=B02&nttId=1',
                       'https://example.invalid/notice/../jobs/view.do?bbsId=B01&nttId=1',
                       'https://example.invalid/notice/%2e%2e/jobs/view.do?bbsId=B01&nttId=1',
                       'https://evil.invalid/notice/view.do?bbsId=B01&nttId=1',
                       'http://example.invalid/notice/view.do?bbsId=B01&nttId=1',
                       'https://user:pass@example.invalid/notice/view.do?bbsId=B01&nttId=1'):
            self.assertFalse(article_in_board(source, detail), detail)
        self.assertFalse(article_in_board(NOTICE, NEWS.replace('list.do', 'view.do') + '&boardId=2'))

    def test_news_scope_filter_rejects_sidebar_detail_but_legacy_contract_remains(self):
        source = dict(name='합성 게시판', url='https://example.invalid/notice/list.do')
        rows = [dict(title='2026년 다른 게시판 모집 안내', link='https://example.invalid/jobs/view.do?nttId=1',
                     _article_context=True),
                dict(title='2026년 등록 게시판 모집 안내', link='https://example.invalid/notice/view.do?nttId=2',
                     _article_context=True)]
        self.assertEqual(len(_filter_article_links(rows, source)), 2)
        self.assertEqual(_filter_article_links(rows, dict(source, news_article_only=True))[0]['link'], rows[1]['link'])
        self.assertEqual(len(_filter_article_links(rows, dict(source, news_article_only=True))), 1)


class PagerTests(unittest.TestCase):
    def test_literal_numeric_page_controls_and_offsets(self):
        source = 'https://example.invalid/notice/list.do?bbsId=B01'
        for key in ('page', 'pageIndex', 'pageNo', 'pageNum', 'currentPage', 'curPage', 'cpage', 'page_no'):
            target = source + '&' + key + '=2'
            self.assertEqual(next_list_page(source, source, [control(target)]), dict(state='next', url=target))
        offset = source + '&article.offset=10'
        self.assertEqual(next_list_page(source, offset, [control(source + '&article.offset=20', '3')])['state'], 'next')

    def test_observed_dbwc_and_dobong_pagers_are_data_only(self):
        target = 'notice_02.php?bm_id=b02&s_field01=&s_field02=&s_txt=&cpage=2'
        self.assertEqual(next_list_page(DBWC, DBWC, [control(target)])['state'], 'next')
        for source in (NOTICE, NEWS, PROGRAM):
            value = next_list_page(source, source, [control('#', onclick='javascript:fnSubmitForm(2);return false;')])
            self.assertEqual(value['state'], 'next')
            self.assertTrue(value['url'].endswith('&pageIndex=2'))
            self.assertNotIn('javascript:', value['url'])

    def test_no_host_board_filter_or_path_expansion_and_no_js_guessing(self):
        source = 'https://example.invalid/notice/list.do?bbsId=B01'
        for target in (source + '&page=2&redirect=https://evil.invalid',
                       source.replace('B01', 'B02') + '&page=2',
                       source.replace('/notice/', '/jobs/') + '&page=2',
                       source.replace('example.invalid', 'evil.invalid') + '&page=2',
                       source + '&page=2&page=3', source + '&page=-1', source + '&page=20',
                       'javascript:goPage(2)', source + '&page=2#frag'):
            self.assertEqual(next_list_page(source, source, [control(target, next=True)])['state'], 'unverified', target)
        for script in ('fnSubmitForm(2);alert(1)', 'fnSubmitForm(2,3)', 'other(2)', 'fnSubmitForm(-2)'):
            self.assertEqual(next_list_page(NOTICE, NOTICE, [control('#', onclick=script)])['state'], 'unverified')
        self.assertFalse(list_page_in_board(source, source.replace('/notice/', '/jobs/')))

    def test_current_disabled_end_and_unavailable_are_distinct(self):
        source = 'https://example.invalid/notice/list.do?page=3'
        self.assertEqual(next_list_page(source, source, [])['state'], 'unverified')
        self.assertEqual(next_list_page(source, source, [control('#', '3', current=True),
            control('#', '다음', next=True, disabled=True)])['state'], 'end')


class FallbackPageTests(unittest.IsolatedAsyncioTestCase):
    def row(self, number):
        return dict(title=f'2026년 신청 모집 {number}', link=f'https://example.invalid/notice/view.do?nttId={number}')

    async def test_reads_three_pages_deduplicates_and_stops_at_configured_cap(self):
        engine = ScraperEngine()
        engine._scrape_fallback = AsyncMock(side_effect=[[self.row(1)], [self.row(1), self.row(2)], [self.row(3)]])
        engine._advance_news_page = AsyncMock(return_value='next')
        rows, status = await engine._scrape_news_fallback_pages(None,
            dict(url='https://example.invalid/notice/list.do', max_pages=3), strict=True, review_status={})
        self.assertEqual((len(rows), status), (3, 'page_limit'))
        self.assertEqual(engine._scrape_fallback.await_count, 3)
        self.assertEqual(engine._advance_news_page.await_count, 2)

    async def test_repeat_or_later_error_keeps_previous_results_as_partial(self):
        for next_rows, expected in ([self.row(1)], 'repeated'), (RuntimeError('synthetic timeout'), 'failed'):
            engine = ScraperEngine()
            engine._scrape_fallback = AsyncMock(side_effect=[[self.row(1)], next_rows])
            engine._advance_news_page = AsyncMock(return_value='next')
            rows, status = await engine._scrape_news_fallback_pages(None,
                dict(url='https://example.invalid/notice/list.do', max_pages=3), strict=True, review_status={})
            self.assertEqual((len(rows), status), (1, expected))

    async def test_unknown_pager_maps_to_existing_partial_result(self):
        page = MagicMock(goto=AsyncMock(), add_init_script=AsyncMock(), close=AsyncMock())
        context = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock())
        engine = ScraperEngine()
        engine.browser = MagicMock(new_context=AsyncMock(return_value=context))
        engine._scrape_fallback = AsyncMock(return_value=[self.row(1)])
        engine._advance_news_page = AsyncMock(return_value='unverified')
        outcome = await engine.scrape_site(dict(name='합성', url='https://example.invalid/notice/list.do',
            max_pages=3, selectors={}, news_article_only=True), with_status=True)
        self.assertEqual((outcome['state'], len(outcome['items'])), ('partial', 1))

    async def test_cancellation_is_not_converted_to_finished_or_failed(self):
        engine = ScraperEngine()
        engine._scrape_fallback = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await engine._scrape_news_fallback_pages(None, dict(url='https://example.invalid/list', max_pages=3),
                strict=True, review_status={})


@unittest.skipUnless(os.getenv('NEWS_BROWSER_SMOKE') == '1', 'Opt-in offline Edge smoke')
class BrowserPagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_and_unknown_script_stop_with_prior_results(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel='msedge', timeout=10000)
            try:
                for mode in ('redirect', 'unknown_script', 'different_board'):
                    seen = []
                    source = 'https://example.invalid/notice/list.do?bbsId=B01'
                    class Proxy:
                        async def new_context(self, **kwargs):
                            context = await browser.new_context(**kwargs)
                            async def route(r):
                                seen.append(r.request.url)
                                if 'pageIndex=2' in r.request.url:
                                    await r.fulfill(status=302, headers={'Location':'https://evil.invalid/list.do'})
                                    return
                                pager = ('<a href="?bbsId=B01&amp;pageIndex=2">2</a>' if mode == 'redirect'
                                    else '<a href="#" onclick="doAnything(2)">2</a>' if mode == 'unknown_script'
                                    else '<a href="/jobs/list.do?bbsId=B01&amp;pageIndex=2">2</a>')
                                await r.fulfill(status=200, content_type='text/html; charset=utf-8', body=
                                    '<table class="board"><tr><td><a href="view.do?bbsId=B01&amp;nttId=1">2026년 합성 지원 모집 안내</a></td></tr></table>'
                                    + '<div class="paginate">' + pager + '</div>')
                            await context.route('**/*', route)
                            return context
                    engine = ScraperEngine(); engine.browser = Proxy()
                    result = await engine.scrape_site(dict(name='합성', url=source, selectors={},
                        max_pages=3, news_article_only=True, fallback_enrich_titles=False), with_status=True)
                    self.assertEqual((result['state'], len(result['items'])), ('partial', 1), mode)
                    self.assertFalse(any('evil.invalid' in url or '/jobs/' in url for url in seen))
                    self.assertEqual(len(seen), 2 if mode == 'redirect' else 1)
            finally:
                await browser.close()

    async def test_real_dom_same_board_three_pages_for_fallback_and_selectors(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel='msedge', timeout=10000)
            try:
                for known in (False, True):
                    source = NOTICE if known else 'https://example.invalid/notice/list.do?bbsId=B01'
                    for selectors in ({}, dict(list_item='.board tr', title='a')):
                        visited = []
                        class Proxy:
                            async def new_context(self, **kwargs):
                                context = await browser.new_context(**kwargs)
                                async def route(r):
                                    parsed = urlsplit(r.request.url)
                                    q = parse_qs(parsed.query)
                                    number = int(q.get('pageIndex', ['1'])[0])
                                    visited.append(number)
                                    article = f'javascript:modify({number})' if known else f'view.do?bbsId=B01&nttId={number}'
                                    pager = (f'<a href="#" onclick="javascript:fnSubmitForm({number+1});return false;">{number+1}</a>'
                                             if known else f'<a href="?bbsId=B01&amp;pageIndex={number+1}"><img alt="다음 페이지"></a>')
                                    html = f'<table class="board"><tr><td><a href="{article}">2026년 지원 모집 안내 {number}</a></td></tr><tr><td><a href="/jobs/view.do?bbsId=B01&amp;nttId=99">2026년 다른 게시판 모집 안내</a></td></tr></table><div class="paginate">{pager}</div>'
                                    await r.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
                                await context.route('**/*', route)
                                return context
                        engine = ScraperEngine()
                        engine.browser = Proxy()
                        result = await engine.scrape_site(dict(name='합성', url=source, selectors=selectors,
                            max_pages=3, news_article_only=True, fallback_enrich_titles=False), with_status=True)
                        self.assertEqual((result['state'], len(result['items'])), ('finished', 3), (known, selectors, result))
                        self.assertEqual(visited, [1, 2, 3])
            finally:
                await browser.close()

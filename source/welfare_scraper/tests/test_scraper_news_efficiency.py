"""Synthetic article/browser tests: no public network, Google calls or user data."""
import asyncio
import copy
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from scraper_engine import ScraperEngine, _filter_article_links
from services import page_coverage


class ArticleScopeTests(unittest.TestCase):
    def setUp(self):
        self.site = {'name': '합성 기관', 'url': 'https://www.example.invalid/board/list.do'}

    def row(self, link, **meta):
        return dict(title='복지 서비스 신청 안내', link=link, date='',
                    _article_context=False, _in_navigation=False, **meta)

    def test_external_partner_and_root_links_are_not_articles(self):
        rows = [self.row('https://www.unicef.org/'), self.row('https://www.samhsa.gov/'),
                self.row('https://example.invalid/'),
                self.row('https://evil.invalid/view.do?nttId=1'),
                self.row('https://example.invalid/board/view.do?nttId=1')]
        result = _filter_article_links(rows, self.site)
        self.assertEqual([x['link'] for x in result], [rows[-1]['link']])
        self.assertFalse(any(key.startswith('_') for key in result[0]))

    def test_same_host_menu_and_footer_are_not_articles(self):
        rows = [self.row('https://example.invalid/intro/subMenu.do?menu=10'),
                {**self.row('https://example.invalid/view.do?nttId=1'), '_in_navigation': True},
                {**self.row('https://example.invalid/notices/summer-application'), '_article_context': True}]
        self.assertEqual([x['link'] for x in _filter_article_links(rows, self.site)], [rows[2]['link']])

    def test_mode_view_with_pagination_and_short_path_article_survive(self):
        rows = [self.row('https://example.invalid/board.do?mode=view&article.offset=0&articleNo=1'),
                self.row('https://example.invalid/post/123')]
        self.assertEqual(len(_filter_article_links(rows, self.site)), 2)

    def test_explicit_cross_host_allowlist_keeps_only_article_links(self):
        self.site['allowed_article_hosts'] = ['partner.invalid']
        rows = [self.row('https://partner.invalid/article/announcement'),
                self.row('https://partner.invalid/'), self.row('https://partner.invalid.evil.invalid/view/1')]
        self.assertEqual([x['link'] for x in _filter_article_links(rows, self.site)], [rows[0]['link']])

    def test_invalid_schemes_credentials_and_duplicates_are_removed(self):
        good = self.row('https://example.invalid/view.do?nttId=1')
        rows = [self.row('javascript:alert(1)'), self.row('file:///view.do'),
                self.row('https://user:pass@example.invalid/view.do'), good, good]
        self.assertEqual(len(_filter_article_links(rows, self.site)), 1)

    def test_navigation_duplicate_does_not_hide_later_article(self):
        good = self.row('https://example.invalid/view.do?nttId=1')
        navigation = {**good, 'title': '메뉴의 동일 주소 바로가기', '_in_navigation': True}
        result = _filter_article_links([navigation, good, good], self.site)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], good['title'])

    def test_news_explicit_selector_scope_rejects_static_menu_but_keeps_announcement(self):
        site = {**self.site, 'news_article_only': True}
        rows = [dict(title='장애인정책 공공요금 감면 상세보기',
                     link='https://example.invalid/board/subMenu.do?menu=1', _article_context=True),
                dict(title='노년 서비스 상세보기',
                     link='https://example.invalid/board/content.do?menu=2', _article_context=True),
                dict(title='공지사항', link='https://example.invalid/board/list.do', _article_context=True),
                dict(title='2026년 지원 모집 안내',
                     link='https://example.invalid/board/content.do?menu=3', _article_context=True),
                dict(title='짧은 제목', link='https://example.invalid/board/post/4', _article_context=True)]
        self.assertEqual([row['title'] for row in _filter_article_links(rows, site)],
                         ['2026년 지원 모집 안내', '짧은 제목'])
        # Outside News the prior explicit-selector path is not filtered by this flag.
        self.assertNotIn('news_article_only', self.site)

    def test_publication_label_survives_scope_filter_but_deadline_is_not_promoted(self):
        rows = [dict(title='지원 신청 모집', link='https://example.invalid/board/post/1',
                     _article_context=True, _publication_text='등록일: 2026-09-02'),
                dict(title='다른 지원 신청 모집', link='https://example.invalid/board/post/2',
                     _article_context=True, _publication_text='마감일: 2026-09-02', date='2026-09-02')]
        values = _filter_article_links(rows, {**self.site, 'news_article_only': True})
        self.assertEqual(values[0]['published_date'], '2026-09-02')
        self.assertNotIn('published_date', values[1])


class SelectorArticleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_only_selector_uses_fallback_and_legacy_behavior_stays_separate(self):
        menu = dict(title='시설 안내 바로가기', link='https://example.invalid/view.do?nttId=5',
                    _article_context=True, _in_navigation=True, _publication_text='등록일: 2026-09-02')
        article = dict(title='2026년 신청 모집 안내', link='https://example.invalid/post/1')
        for news_only in (True, False):
            with self.subTest(news_only=news_only):
                page = MagicMock(goto=AsyncMock(), add_init_script=AsyncMock(),
                    wait_for_selector=AsyncMock(), evaluate=AsyncMock(return_value=[menu]), close=AsyncMock())
                context = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock())
                engine = ScraperEngine()
                engine.browser = MagicMock(new_context=AsyncMock(return_value=context))
                engine._scrape_fallback = AsyncMock(return_value=[article])
                site = dict(name='합성 기관', url='https://example.invalid/list', max_pages=1,
                            selectors={'list_item': 'tr', 'title': 'a'}, news_article_only=news_only)
                result = await engine.scrape_site(site, known_links=set(), with_status=True)
                self.assertEqual(result['items'][0]['link'], article['link'] if news_only else menu['link'])
                self.assertFalse(any(key.startswith('_') for key in result['items'][0]))
                if news_only:
                    engine._scrape_fallback.assert_awaited_once()
                else:
                    engine._scrape_fallback.assert_not_awaited()


class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.url = 'https://example.invalid/view.do?nttId=1'
        self.parsed = dict(text='공개 게시글 신청 방법과 자격 안내 ' * 15, policy_text='본문', title='지원 안내',
                           images=[], attachments=[], attachment_items=[], source_page=page_coverage.make())
        self.parser = MagicMock(side_effect=lambda *args: copy.deepcopy(self.parsed))
        previous_parser_module = sys.modules.get('ai_classifier')
        sys.modules['ai_classifier'] = SimpleNamespace(_parse_page_html=self.parser)
        def restore_parser_module():
            if previous_parser_module is None:
                sys.modules.pop('ai_classifier', None)
            else:
                sys.modules['ai_classifier'] = previous_parser_module
        self.addCleanup(restore_parser_module)
        self.page = MagicMock(url=self.url, frames=[], main_frame=object())
        self.page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.page.content = AsyncMock(return_value='<main>synthetic public article</main>')
        self.page.add_init_script = AsyncMock()
        self.page.wait_for_timeout = AsyncMock()
        self.page.query_selector = AsyncMock(return_value=None)
        self.page.query_selector_all = AsyncMock(return_value=[])
        self.context = MagicMock(new_page=AsyncMock(return_value=self.page), close=AsyncMock(),
                                 route=AsyncMock(), route_web_socket=AsyncMock())
        self.engine = ScraperEngine()
        self.engine.browser = MagicMock(new_context=AsyncMock(return_value=self.context))
        self.validator = AsyncMock(side_effect=lambda value: value)
        self.engine.capture_page_vision = AsyncMock(side_effect=AssertionError('No Google vision permitted'))
        self.engine._scrape_vision_list = AsyncMock(side_effect=AssertionError('No Google vision permitted'))

    async def recover(self, **kwargs):
        return await self.engine.fetch_rendered_page(self.url, url_validator=self.validator, **kwargs)

    async def test_dom_recovers_normal_schema_and_strict_tls_without_ai(self):
        self.parsed.update(images=['https://example.invalid/poster.jpg'],
                           attachments=['https://example.invalid/file.hwpx'],
                           attachment_items=[dict(url='https://example.invalid/file.hwpx', kind='hwpx')])
        result = await self.recover()
        self.assertEqual(result['attachment_items'], self.parsed['attachment_items'])
        self.assertEqual(result['images'], self.parsed['images'])
        self.assertTrue(result['rendered_locally'])
        self.assertFalse(self.engine.browser.new_context.call_args.kwargs['ignore_https_errors'])
        self.context.close.assert_awaited_once()
        self.engine.capture_page_vision.assert_not_called()
        self.page.query_selector.assert_not_awaited()

    async def test_http_denial_is_not_read_or_captured(self):
        self.page.goto.return_value.status = 403
        self.assertIsNone(await self.recover())
        self.page.content.assert_not_awaited()
        self.context.close.assert_awaited_once()

    async def test_login_captcha_and_access_denied_are_not_recovered(self):
        for title in ('로그인', 'CAPTCHA', 'Access denied'):
            with self.subTest(title=title):
                self.parsed['title'] = title
                self.assertIsNone(await self.recover())
        self.page.query_selector.assert_not_awaited()

    async def test_initial_private_url_is_blocked_before_browser(self):
        self.validator.side_effect = ValueError('private')
        self.assertIsNone(await self.recover())
        self.engine.browser.new_context.assert_not_awaited()

    async def test_redirect_or_subresource_private_url_is_blocked(self):
        def validate(value):
            if '127.0.0.1' in value:
                raise ValueError('private')
            return value
        self.validator.side_effect = validate
        await self.recover()
        guard = self.context.route.call_args.args[1]
        route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        request = SimpleNamespace(url='http://127.0.0.1/internal', method='GET', resource_type='xhr')
        await guard(route, request)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
        self.page.url = 'http://127.0.0.1/internal'
        self.assertIsNone(await self.recover())

    async def test_unsafe_post_and_websockets_are_blocked(self):
        await self.recover()
        guard = self.context.route.call_args.args[1]
        route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        request = SimpleNamespace(url=self.url, method='POST', resource_type='xhr')
        await guard(route, request)
        route.abort.assert_awaited_once()
        socket = SimpleNamespace(close=AsyncMock())
        await self.context.route_web_socket.call_args.args[1](socket)
        socket.close.assert_awaited_once()

    async def test_timeout_closes_context(self):
        self.page.goto.side_effect = asyncio.TimeoutError
        self.assertIsNone(await self.recover())
        self.context.close.assert_awaited_once()

    async def test_cached_origin_still_rejects_credentials_and_oversized_url(self):
        await self.recover()
        guard = self.context.route.call_args.args[1]
        valid = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        await guard(valid, SimpleNamespace(url=self.url, method='GET', resource_type='xhr'))
        valid.continue_.assert_awaited_once()
        for value in ('https://user:password@example.invalid/private', self.url + '&q=' + 'x' * 10000):
            route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
            await guard(route, SimpleNamespace(url=value, method='GET', resource_type='xhr'))
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()

    async def test_image_only_and_duplicate_text_iframe_keep_new_attachments(self):
        for text in ('', self.parsed['text']):
            with self.subTest(text_empty=not text):
                frame = SimpleNamespace(url='https://example.invalid/frame', content=AsyncMock(return_value='frame'))
                self.page.frames = [frame]
                extra = dict(text=text, policy_text=text, title='', images=['https://example.invalid/poster.png'],
                             attachments=['https://example.invalid/notice.pdf'],
                             attachment_items=[dict(url='https://example.invalid/notice.pdf', kind='pdf')],
                             source_page=page_coverage.make('no_text') if not text else page_coverage.make())
                self.parser.side_effect = lambda html, url: copy.deepcopy(extra if html == 'frame' else self.parsed)
                result = await self.recover()
                self.assertEqual(result['images'], extra['images'])
                self.assertEqual(result['attachments'], extra['attachments'])
                self.assertEqual(result['attachment_items'], extra['attachment_items'])
                if not text:
                    self.assertIn('no_text', result['source_page']['reasons'])

    async def test_small_canvas_uses_only_local_ocr_and_keeps_partial_coverage(self):
        self.parsed.update(text='', policy_text='', source_page=page_coverage.make('no_text'))
        canvas = SimpleNamespace(bounding_box=AsyncMock(return_value=dict(width=800, height=600)),
                                 screenshot=AsyncMock(return_value=b'synthetic-canvas'))
        self.page.query_selector.return_value = canvas
        with patch('services.local_document_process.extract_document_isolated', new_callable=AsyncMock) as ocr:
            ocr.return_value = SimpleNamespace(text='캔버스의 신청 기간 안내')
            result = await self.recover()
        self.assertIn('캔버스', result['text'])
        self.assertEqual(result['source_page']['status'], 'review_required')
        self.assertIn('local_ocr', result['source_page']['methods'])
        ocr.assert_awaited_once()
        self.assertEqual(ocr.call_args.kwargs['timeout'], 12)
        self.engine.capture_page_vision.assert_not_called()

    async def test_existing_poster_skips_duplicate_canvas_ocr(self):
        self.parsed.update(text='', images=['https://example.invalid/poster.png'])
        result = await self.recover()
        self.assertIsNotNone(result)
        self.page.query_selector.assert_not_awaited()
        self.page.query_selector_all.assert_not_awaited()

    async def test_explicit_failed_poster_recovery_captures_one_loaded_selected_image(self):
        url = 'https://example.invalid/poster.png'
        self.parsed.update(text='', images=[url], source_page=page_coverage.make('no_text'))
        poster = SimpleNamespace(evaluate=AsyncMock(return_value=dict(src=url, loaded=True, excluded=False)),
                                 bounding_box=AsyncMock(return_value=dict(width=800, height=600)),
                                 screenshot=AsyncMock(return_value=b'synthetic-poster'))
        second = SimpleNamespace(evaluate=AsyncMock(), screenshot=AsyncMock())
        self.page.query_selector_all.return_value = [poster, second]
        with patch('services.local_document_process.extract_document_isolated', new_callable=AsyncMock) as ocr:
            ocr.return_value = SimpleNamespace(text='포스터 신청 기간과 대상 안내')
            result = await self.recover(capture_images=True)
        self.assertEqual(result['local_capture_image_url'], url)
        self.assertIn('local_ocr', result['source_page']['methods'])
        self.assertIn('rendered_capture_partial', result['source_page']['reasons'])
        self.assertNotIn('_captured_poster_url', result)
        second.evaluate.assert_not_awaited()
        ocr.assert_awaited_once()
        self.engine.capture_page_vision.assert_not_called()

    async def test_poster_capture_rejects_unloaded_unselected_captcha_and_oversize(self):
        url = 'https://example.invalid/poster.png'
        self.parsed.update(text='', images=[url])
        cases = [(dict(src=url, loaded=False, excluded=False), dict(width=800, height=600)),
                 (dict(src='https://example.invalid/logo.png', loaded=True, excluded=False), dict(width=800, height=600)),
                 (dict(src=url, loaded=True, excluded=True), dict(width=800, height=600)),
                 (dict(src=url, loaded=True, excluded=False), dict(width=800, height=20000))]
        for info, box in cases:
            with self.subTest(info=info, box=box):
                poster = SimpleNamespace(evaluate=AsyncMock(return_value=info),
                                         bounding_box=AsyncMock(return_value=box), screenshot=AsyncMock())
                self.page.query_selector_all.return_value = [poster]
                result = await self.recover(capture_images=True)
                self.assertNotIn('local_capture_image_url', result)
                self.assertNotIn('local_ocr', result['source_page'].get('methods', []))
                poster.screenshot.assert_not_awaited()

    async def test_truthy_nonboolean_does_not_enable_poster_capture(self):
        self.parsed.update(text='', images=['https://example.invalid/poster.png'])
        await self.recover(capture_images='true')
        self.page.query_selector_all.assert_not_awaited()

    async def test_oversized_canvas_is_not_captured(self):
        self.parsed.update(text='', images=[])
        canvas = SimpleNamespace(bounding_box=AsyncMock(return_value=dict(width=800, height=20000)),
                                 screenshot=AsyncMock())
        self.page.query_selector.return_value = canvas
        self.assertIsNone(await self.recover())
        canvas.screenshot.assert_not_awaited()

    async def test_new_news_path_can_skip_duplicate_title_fetch(self):
        rows = [dict(title='실제 게시글의 안내 제목', link=self.url)]
        self.engine._scrape_link_based = AsyncMock(return_value=rows)
        self.engine._enrich_titles = AsyncMock(side_effect=AssertionError('Duplicate detail request'))
        result = await self.engine._scrape_fallback(self.page,
            dict(name='합성 기관', url=self.url, fallback_enrich_titles=False))
        self.assertEqual(result, rows)
        self.engine._enrich_titles.assert_not_awaited()


@unittest.skipUnless(os.getenv('NEWS_BROWSER_SMOKE') == '1', 'Opt-in offline Edge smoke')
class OfflineBrowserScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dom_excludes_navigation_and_keeps_board_articles(self):
        from playwright.async_api import async_playwright
        html = '''<html><head><meta charset="utf-8"></head><body>
            <nav><a href="/view.do?nttId=999">기관 소개 상세 메뉴 바로가기</a></nav>
            <header><a href="/view.do?nttId=777">전체 사이트 상단 메뉴 바로가기</a></header>
            <nav><a href="/view.do?nttId=2">메뉴의 동일 주소 바로가기</a></nav>
            <main><table class="board-list"><tr><td><a href="/view.do?nttId=1">2026년 지원 신청 안내</a></td></tr></table>
              <article><header><h2><a href="/news/2026/123">게시글 헤더의 실제 공고 제목</a></h2></header></article>
              <article><a href="/view.do?nttId=2">본문의 실제 공고 제목</a></article>
              <article><a href="/view.do?nttId=2">본문의 중복 공고 제목</a></article>
              <a href="/board.do?mode=view&amp;article.offset=0">신규 서비스 신청 안내</a>
              <ul><li><a href="/notices/summer-program">여름 프로그램 신청 안내</a><time>2026-09-17</time></li></ul>
              <a href="/intro/subMenu.do?menu=20">기관의 담당 부서 전체 소개</a>
            </main>
            <footer><a href="https://unicef.org/">유니세프 국제기구 공식 홈페이지</a>
              <a href="https://www.samhsa.gov/">해외 공공기관 공식 홈페이지</a></footer>
            </body></html>'''
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True, channel='msedge', timeout=10000)
            except Exception:
                browser = await playwright.chromium.launch(headless=True, channel='chrome', timeout=10000)
            try:
                context = await browser.new_context()
                # This synthetic body is UTF-8. Without an explicit charset,
                # Korean Windows Edge may decode it as EUC-KR before scraping.
                await context.route('**/*', lambda route: route.fulfill(
                    status=200, content_type='text/html; charset=utf-8', body=html))
                page = await context.new_page()
                await page.goto('https://example.invalid/list')
                self.assertEqual(await page.evaluate('document.characterSet'), 'UTF-8')
                engine = ScraperEngine()
                rows = await engine._scrape_link_based(page, {'name': '합성 기관', 'url': page.url})
                self.assertEqual(len(rows), 5)
                self.assertTrue(all('example.invalid' in row['link'] for row in rows))
                self.assertTrue(any('mode=view' in row['link'] for row in rows))
                self.assertFalse(any('999' in row['link'] for row in rows))
                self.assertFalse(any('777' in row['link'] for row in rows))
                self.assertTrue(any(row['title'] == '게시글 헤더의 실제 공고 제목' for row in rows))
                duplicate = [row for row in rows if 'nttId=2' in row['link']]
                self.assertEqual(len(duplicate), 1)
                self.assertEqual(duplicate[0]['title'], '본문의 실제 공고 제목')
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()

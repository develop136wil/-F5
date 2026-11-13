"""Known-board URL adapters: no AI, accounts or public network in these tests."""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.board_detail_links import board_kind, resolve_script_detail, materialize_board_links
from scraper_engine import ScraperEngine

DBWC = 'http://www.dbwc2017.or.kr/site/notice_02.php'
NOTICE = 'https://welfare.dobong.go.kr/dobongBokji/bbs/BMSR00001/list.do?menuNo=200098'
NEWS = 'https://welfare.dobong.go.kr/dobongBokji/bbs/BMSR00002/list.do?menuNo=200099'
PROGRAM = 'https://welfare.dobong.go.kr/dobongBokji/pgm/progrmReqst/list.do?menuNo=200086'


class DetailLinkTests(unittest.TestCase):
    def test_known_functions_only_map_to_their_own_board(self):
        cases = [(DBWC, "javascript:void go_view('624')", '?bmode=view&bm_id=b02&idx=624'),
                 (NOTICE, "javascript:modify('2067');", '/view.do?menuNo=200098&boardId=2067'),
                 (NEWS, 'javascript:modify("2069");', '/view.do?menuNo=200099&boardId=2069'),
                 (PROGRAM, 'moveToView(547); return false;', '/view.do?menuNo=200086&reqstId=547')]
        for source, call, suffix in cases:
            with self.subTest(source=source):
                value = resolve_script_detail(source, source, call)
                self.assertTrue(value.endswith(suffix), value)
                self.assertNotIn('javascript', value)

    def test_onclick_and_duplicate_equal_calls_are_supported(self):
        expected = resolve_script_detail(NOTICE, NOTICE, "javascript:modify('42')")
        self.assertEqual(resolve_script_detail(NOTICE, NOTICE, '#', "modify('42'); return false;"), expected)
        self.assertEqual(resolve_script_detail(NOTICE, NOTICE, "javascript:modify('42')", 'modify(42)'), expected)

    def test_conflicting_href_and_onclick_are_rejected(self):
        self.assertEqual(resolve_script_detail(NOTICE, NOTICE, "javascript:modify('42')", 'modify(43)'), '')
        self.assertEqual(resolve_script_detail(NOTICE, NOTICE, '/other/view.do?id=1', 'modify(42)'), '')

    def test_malformed_executable_and_non_numeric_inputs_are_rejected(self):
        for call in ("modify('1');alert(1)", "modify('1&menuNo=9')", "modify('../1')", 'modify(-1)',
                     'modify(0)', 'modify(01)', 'modify(1.5)', 'modify(1e3)', 'modify(１)',
                     'modify(9999999999999999999)', "modify('1');location.href='https://evil.invalid'",
                     'eval("modify(1)")', 'window.modify(1)', 'other(1)', 'modify(1,2)',
                     'modify(1)/*comment*/', 'modify(1);return true;', 'modify(1)\x00'):
            with self.subTest(call=call):
                self.assertEqual(resolve_script_detail(NOTICE, NOTICE, call), '')

    def test_function_from_another_board_is_rejected(self):
        for source, call in [(DBWC, 'modify(1)'), (NOTICE, 'go_view(1)'),
                             (NEWS, 'moveToView(1)'), (PROGRAM, 'modify(1)')]:
            self.assertEqual(resolve_script_detail(source, source, call), '')

    def test_unknown_host_path_port_and_credentials_are_rejected(self):
        for source in (NOTICE.replace('welfare.dobong.go.kr', 'evil.invalid'),
                       NOTICE.replace('welfare.dobong.go.kr', 'welfare.dobong.go.kr.evil.invalid'),
                       NOTICE.replace('welfare.dobong.go.kr', 'user:pass@welfare.dobong.go.kr'),
                       NOTICE.replace('welfare.dobong.go.kr', 'welfare.dobong.go.kr:444'),
                       NOTICE.replace('BMSR00001', 'BMSR00003'),
                       NOTICE.replace('list.do', 'other.do'), NOTICE.replace('https:', 'file:'),
                       NOTICE + '#extra', NOTICE + '\n', NOTICE.replace('/bbs/', '/bbs/../bbs/')):
            self.assertEqual(resolve_script_detail(source, source, 'modify(42)'), '')

    def test_wrong_menu_duplicate_query_and_redirected_board_are_rejected(self):
        for source in (NOTICE.replace('200098', '200099'), NOTICE + '&menuNo=200099',
                       DBWC + '?bm_id=b01', DBWC + '?bmode=view&idx=42'):
            self.assertEqual(board_kind(source), '')
        self.assertEqual(resolve_script_detail(NOTICE, NEWS, 'modify(42)'), '')
        self.assertEqual(resolve_script_detail(NOTICE, 'https://evil.invalid/list.do', 'modify(42)'), '')

    def test_tracking_parameters_are_not_copied_to_generated_links(self):
        result = resolve_script_detail(NOTICE + '&utm_source=example', NOTICE + '&pageIndex=2', 'modify(42)')
        self.assertTrue(result.endswith('/view.do?menuNo=200098&boardId=42'))
        self.assertNotIn('utm_', result)
        self.assertNotIn('pageIndex', result)

    def test_dom_metadata_is_removed_and_ordinary_links_are_unchanged(self):
        ordinary = dict(title='일반 게시글', link='https://example.invalid/view/4')
        scripted = dict(title='새로운 게시글', link='', _board_page=NOTICE, _board_script=True,
                        _board_href='javascript:modify(42)', _board_onclick='', _in_navigation=False)
        rows = materialize_board_links([ordinary, scripted], NOTICE)
        self.assertEqual(rows[0], ordinary)
        self.assertFalse(any(key.startswith('_board_') for key in rows[1]))
        self.assertEqual(rows[1]['_in_navigation'], False)
        self.assertTrue(rows[1]['link'].endswith('boardId=42'))

    def test_program_card_only_allows_its_exact_detail_path_and_parameters(self):
        def resolve(link, page=PROGRAM):
            return materialize_board_links([dict(title='프로그램 모집 제목', link=link,
                _board_page=page, _board_card=True)], PROGRAM)[0]['link']
        self.assertTrue(resolve('./view.do?menuNo=200086&reqstId=547').endswith('reqstId=547'))
        for link in ('./view.do?menuNo=200099&reqstId=547', './view.do?menuNo=200086&reqstId=x',
                     './view.do?menuNo=200086&reqstId=547&reqstId=2',
                     './view.do?menuNo=200086&reqstId=547&next=https://evil.invalid',
                     '/dobongBokji/bbs/BMSR00001/view.do?menuNo=200086&reqstId=547',
                     'https://evil.invalid/view.do?menuNo=200086&reqstId=547', 'javascript:moveToView(547)'):
            self.assertEqual(resolve(link), '', link)
        self.assertEqual(resolve('./view.do?menuNo=200086&reqstId=547', NOTICE), '')


class ExtractionPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_board_keeps_post_title_instead_of_generic_html_title(self):
        engine = ScraperEngine()
        rows = [dict(title='2026년 실제 지원 모집 안내', link=NOTICE.replace('list.do', 'view.do') + '&boardId=42')]
        engine._scrape_link_based = AsyncMock(return_value=rows)
        engine._enrich_titles = AsyncMock(side_effect=AssertionError('Known list title must be preserved'))
        self.assertEqual(await engine._scrape_fallback(None, dict(name='합성 기관', url=NOTICE)), rows)
        engine._enrich_titles.assert_not_awaited()

    async def test_fallback_and_selector_paths_both_materialize_known_script_links(self):
        row = dict(title='2026년 실제 게시글 지원 모집', link='', _board_page=NOTICE,
                   _board_script=True, _board_href='javascript:modify(42)', _board_onclick='',
                   _article_context=True, _in_navigation=False)
        for selectors in ({}, {'list_item':'tr', 'title':'a'}):
            page = MagicMock(goto=AsyncMock(), add_init_script=AsyncMock(),
                wait_for_selector=AsyncMock(), evaluate=AsyncMock(return_value=[row]))
            context = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock())
            engine = ScraperEngine()
            engine.browser = MagicMock(new_context=AsyncMock(return_value=context))
            engine._enrich_titles = AsyncMock(side_effect=AssertionError('No detail prefetch'))
            result = await engine.scrape_site(dict(name='합성 기관', url=NOTICE,
                selectors=selectors, max_pages=1, news_article_only=True, fallback_enrich_titles=False), with_status=True)
            self.assertEqual(len(result['items']), 1)
            self.assertTrue(result['items'][0]['link'].endswith('boardId=42'))
            self.assertFalse(any(key.startswith('_') for key in result['items'][0]))


@unittest.skipUnless(os.getenv('NEWS_BROWSER_SMOKE') == '1', 'Opt-in offline Edge smoke')
class OfflineBoardDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_dom_readers_do_not_execute_calls_and_recover_each_board(self):
        from playwright.async_api import async_playwright
        cases = [(DBWC, "javascript:void go_view('624')", 'idx=624'),
                 (NOTICE, "javascript:modify('2067');", 'boardId=2067'),
                 (NEWS, "javascript:modify('2069');", 'boardId=2069')]
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel='msedge', timeout=10000)
            try:
                engine = ScraperEngine()
                # Route at browser level because scrape_site creates its own context.
                class RoutedBrowser:
                    async def new_context(self, **kwargs):
                        context = await browser.new_context(**kwargs)
                        await context.route('**/*', lambda route: route.fulfill(status=200,
                            content_type='text/html; charset=utf-8', body=html))
                        return context
                engine.browser = RoutedBrowser()
                for url, call, expected in cases:
                    html = f'''<table class="board"><tr><td><a href="{call}">2026년 실제 지원 모집 안내<span class="bbs-ico new_g">NEW</span></a></td></tr>
                        <tr><td><a href="javascript:modify('1');alert(1)">실행하면 안 되는 공격 안내</a></td></tr></table>
                        <nav><a href="{call}">머리글 메뉴 중복 링크</a></nav>'''
                    for selectors in ({}, {'list_item':'.board tr', 'title':'a'}):
                        result = await engine.scrape_site(dict(name='합성 기관', url=url,
                            selectors=selectors, max_pages=1, news_article_only=True,
                            fallback_enrich_titles=False), with_status=True)
                        self.assertEqual(len(result['items']), 1, (url, selectors, result))
                        self.assertTrue(result['items'][0]['link'].endswith(expected))
                        self.assertEqual(result['items'][0]['title'], '2026년 실제 지원 모집 안내')
                html = '''<div class="item"><div class="cont"><p class="tit">2026년 실제 프로그램 모집 안내</p>
                    <a href="./view.do?menuNo=200086&amp;reqstId=547">자세히보기</a></div></div>
                    <div class="item"><div class="cont"><p class="tit">범위 밖으로 나가는 프로그램</p>
                    <a href="https://evil.invalid/view.do?menuNo=200086&amp;reqstId=9">자세히보기</a></div></div>'''
                for selectors in ({}, {'list_item':'.item', 'title':'.tit'}):
                    result = await engine.scrape_site(dict(name='합성 기관', url=PROGRAM,
                        selectors=selectors, max_pages=1, news_article_only=True,
                        fallback_enrich_titles=False), with_status=True)
                    self.assertEqual(len(result['items']), 1, result)
                    self.assertEqual(result['items'][0]['title'], '2026년 실제 프로그램 모집 안내')
                    self.assertTrue(result['items'][0]['link'].endswith('reqstId=547'))
            finally:
                await browser.close()

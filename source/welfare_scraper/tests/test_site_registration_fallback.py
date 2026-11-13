import unittest
import ast
import asyncio
import logging
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services.site_registration import prepare_site, has_post_evidence, is_likely_homepage_url


class RegistrationFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_homepage_detection_does_not_reject_query_boards(self):
        for url in ('https://example.org', 'https://example.org/',
                    'https://example.org/index.html', 'https://example.org/MAIN.DO#top',
                    'https://example.org/default.aspx'):
            self.assertTrue(is_likely_homepage_url(url), url)
        for url in ('https://example.org/board', 'https://example.org/board.do?menu=101',
                    'https://example.org/index.php?board=notice', 'https://example.org/?page=notice',
                    'https://example.org/posts/1', 'not a URL', 'ftp://example.org/'):
            self.assertFalse(is_likely_homepage_url(url), url)

    async def test_home_menu_selectors_do_not_claim_registration_verified(self):
        probe = AsyncMock(return_value=False)
        site = await prepare_site('https://example.org/',
                                  {'success': True, 'list_item': 'nav li', 'title': 'a'}, probe)
        self.assertEqual(site['registration_status'], 'needs_check')
        self.assertEqual(site['selectors'], {})
        probe.assert_awaited_once()
        self.assertEqual(probe.call_args.args[0]['selectors'], {})

    async def test_real_home_page_post_list_remains_supported(self):
        site = await prepare_site('https://example.org/',
                                  {'success': True, 'list_item': 'li', 'title': 'a'},
                                  AsyncMock(return_value=True))
        self.assertEqual(site['registration_status'], 'basic_verified')
        self.assertEqual(site['url'], 'https://example.org/')

    async def test_query_based_board_keeps_verified_rules(self):
        probe = AsyncMock()
        site = await prepare_site('https://example.org/index.do?menu=notice',
                                  {'success': True, 'list_item': 'li', 'title': 'a'}, probe)
        self.assertEqual(site['registration_status'], 'rules_ready')
        probe.assert_not_awaited()

    async def test_failed_analysis_retains_address(self):
        probe = AsyncMock(return_value=False)
        site = await prepare_site('https://example.org/board', {'success': False}, probe)
        self.assertEqual(site['registration_status'], 'needs_check')
        self.assertEqual(site['selectors'], {})
        self.assertEqual(site['url'], 'https://example.org/board')

    async def test_verified_basic_collection(self):
        site = await prepare_site('https://example.org/board', None, AsyncMock(return_value=True))
        self.assertEqual(site['registration_status'], 'basic_verified')

    async def test_rules_skip_fallback(self):
        probe = AsyncMock()
        site = await prepare_site('https://example.org/board',
                                  {'success': True, 'list_item': 'li', 'title': 'a'}, probe)
        self.assertEqual(site['registration_status'], 'rules_ready')
        probe.assert_not_called()

    async def test_empty_rules_use_fallback(self):
        probe = AsyncMock(return_value=False)
        site = await prepare_site('https://example.org/board', {'success': True}, probe)
        self.assertEqual(site['registration_status'], 'needs_check')
        probe.assert_awaited_once()

    def test_menu_and_empty_body_are_not_verified(self):
        item = {'title': '기관 소개 페이지', 'link': 'https://example.org/about'}
        self.assertFalse(has_post_evidence(item, {'text': item['title'] * 40}))
        item['link'] = 'https://example.org/board?seq=123'
        self.assertFalse(has_post_evidence(item, {'text': ''}))
        self.assertFalse(has_post_evidence(item, {'text': '로그인이 필요합니다 ' + item['title'] * 40}))
        self.assertTrue(has_post_evidence(item, {'text': item['title'] + ' 지원 안내 내용' * 40}))


class BatchRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, results, *, start_error=False, cancel=False, names=None):
        tree = ast.parse((Path(__file__).parents[1] / 'api_server.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_batch_sites')
        saved = []
        event = asyncio.Event()
        async def analyze(url):
            if cancel:
                event.set()
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        service = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError() if start_error else None),
                                  stop=AsyncMock(), analyze_site=AsyncMock(side_effect=analyze))
        async def run_db(fn, *args):
            return fn(*args)
        env = {'List': list, 'BATCH_CANCEL_EVENT': event, 'BATCH_STATUS': {'is_running': True},
               'site_analyzer': SimpleNamespace(SiteAnalyzerService=lambda: service),
               'database': SimpleNamespace(run_db=run_db), 'logger': logging.getLogger('test'),
               'utils': SimpleNamespace(load_sites=lambda: saved, save_sites=lambda items: saved.extend(items),
                                        validate_site=lambda site: (True, ''))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<batch>', 'exec'), env)
        coordinator = SimpleNamespace(finish_job=lambda name: None)
        async def prepare(url, result):
            return await prepare_site(url, result, AsyncMock(return_value=False))
        with patch.dict(sys.modules, {'services.ai_coordinator': coordinator}), \
             patch('services.site_registration.prepare_site', side_effect=prepare):
            await env['process_batch_sites'](['https://example.org/a', 'https://example.org/b'], names)
        return saved, env['BATCH_STATUS'], service

    async def test_failures_still_saved(self):
        saved, status, service = await self.run_batch([RuntimeError('analysis unavailable'), {'success': False}])
        self.assertEqual(len(saved), 2)
        self.assertEqual(status['review_count'], 2)
        self.assertEqual(status['fail_count'], 0)
        self.assertFalse(status['is_running'])
        service.stop.assert_awaited_once()

    async def test_imported_name_preserved_when_analysis_fails(self):
        saved, _, _ = await self.run_batch([{'success': False}, {'success': False}], names={'https://example.org/a':'우리 기관'})
        self.assertEqual(saved[0]['name'], '우리 기관')
        self.assertEqual(saved[1]['name'], 'example.org')

    async def test_limit_stops_ai_but_retains_remaining_addresses(self):
        saved, status, service = await self.run_batch([{'error': '일일 AI 분석 한도'}])
        self.assertEqual(len(saved), 2)
        self.assertEqual(service.analyze_site.await_count, 1)

    async def test_browser_failure_still_saves(self):
        saved, status, service = await self.run_batch([], start_error=True)
        self.assertEqual(len(saved), 2)
        service.analyze_site.assert_not_awaited()

    async def test_cancel_does_not_register_remaining_addresses(self):
        saved, status, service = await self.run_batch([{'success': False}], cancel=True)
        self.assertEqual(saved, [])
        self.assertTrue(status['cancelled'])


class RegistrationStorageTests(unittest.TestCase):
    def test_migration_and_upsert_preserve_sites_and_status(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT, url TEXT UNIQUE, base_url TEXT, selectors TEXT, max_pages INTEGER)')
        conn.execute("INSERT INTO sites (name,url) VALUES ('existing','https://example.org/old')")
        source = (Path(__file__).parents[1] / 'database.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        init = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'init_db')
        migration = next(n for n in ast.walk(init) if isinstance(n, ast.If) and "'registration_status' not in columns" in ast.unparse(n.test))
        for _ in range(2):
            env = {'columns': {r[1] for r in conn.execute('PRAGMA table_info(sites)')}, 'cursor': conn.cursor()}
            exec(compile(ast.Module(body=[migration], type_ignores=[]), '<migration>', 'exec'), env)
        tree = ast.parse((Path(__file__).parents[1] / 'utils.py').read_text(encoding='utf-8'))
        save = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'save_sites')
        save.args.args[0].annotation = None
        env = {'database': SimpleNamespace(get_connection=lambda: conn), 'json': json,
               'validate_site': lambda site: (True, ''), 'logger': logging.getLogger('test')}
        exec(compile(ast.Module(body=[save], type_ignores=[]), '<save>', 'exec'), env)
        with patch.dict(sys.modules, {'services.news_default_condition': SimpleNamespace(synchronize_sites=lambda c: None)}):
            env['save_sites']([{'name': 'new', 'url': 'https://example.org/new', 'registration_status': 'needs_check'}])
            env['save_sites']([{'name': 'edited', 'url': 'https://example.org/new'}])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sites').fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT registration_status FROM sites WHERE name='edited'").fetchone()[0], 'needs_check')


if __name__ == '__main__':
    unittest.main()

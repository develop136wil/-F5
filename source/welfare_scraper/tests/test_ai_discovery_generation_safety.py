"""Offline integrity checks for selector analysis and manually reviewed vision.

Screenshots, browser contexts, provider calls and admission are synthetic.
Use scripts/run_isolated_tests.py, never a real app profile.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import types

import site_analyzer
from scraper_engine import ScraperEngine
from services import ai_coordinator
from services.ai_generation_safety import (
    AiResponseReviewRequired, SELECTOR_OUTPUT_TOKENS, VISION_OUTPUT_TOKENS,
)
from tests.test_ai_generation_safety import response


class TestGenerationInventory(unittest.TestCase):
    def test_all_known_production_sdk_calls_have_caps_and_completion_guard(self):
        root = Path(__file__).resolve().parents[1]
        expected = {'ai_classifier.py': 1, 'site_analyzer.py': 1, 'scraper_engine.py': 2,
                    'services/ocr_service.py': 1, 'services/policy_sync.py': 1}
        for path, count in expected.items():
            tree = ast.parse((root / path).read_text(encoding='utf-8-sig'))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == 'generate_content']
            self.assertEqual(len(calls), count, path)
            for call in calls:
                config = next((keyword.value for keyword in call.keywords if keyword.arg == 'config'), None)
                self.assertIsInstance(config, ast.Call, path)
                self.assertIn('max_output_tokens', [keyword.arg for keyword in config.keywords], path)
                owner = next(node for node in ast.walk(tree)
                             if isinstance(node, ast.AsyncFunctionDef) and call in ast.walk(node))
                guards = [node for node in ast.walk(owner) if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name) and node.func.id == 'complete_response_text']
                self.assertTrue(guards, path)


class TestDiscoveryGeneration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.generate = AsyncMock(return_value=response('[]'))
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=self.generate)))
        self.site = {'name': '합성 공공기관', 'url': 'https://example.invalid/board', 'selectors': {}}
        self.page = MagicMock()
        for name in ('screenshot', 'add_init_script', 'goto', 'close'):
            setattr(self.page, name, AsyncMock())
        self.page.screenshot.return_value = b'synthetic-public-board-not-personal-data'
        self.context = MagicMock(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.engine = ScraperEngine()
        self.engine.browser = MagicMock(new_context=AsyncMock(return_value=self.context))
        patch.object(site_analyzer, 'get_genai_client', return_value=self.client).start()
        patch.object(site_analyzer._site_rate_limiter, 'acquire', new_callable=AsyncMock).start()
        patch.object(ai_coordinator, 'create_client', return_value=self.client).start()
        self.admit = patch.object(ai_coordinator, 'acquire_model_slot', new_callable=AsyncMock).start()
        patch('scraper_engine._vision_rate_limiter.acquire', new_callable=AsyncMock).start()
        patch('scraper_engine.asyncio.sleep', new_callable=AsyncMock).start()
        patch.dict('os.environ', {'GOOGLE_API_KEY': 'synthetic-offline-test-key'}).start()
        self.addCleanup(patch.stopall)

    def assert_cap(self, expected):
        config = self.generate.await_args.kwargs['config']
        self.assertEqual(config.max_output_tokens, expected)
        self.assertEqual(config.candidate_count, 1)

    async def analyze(self):
        return await site_analyzer.analyze_with_ai(
            '<table><tbody><tr><td><a href="/notice">지원사업 안내</a></td></tr></tbody></table>',
            self.site['url'])

    async def test_selector_success_keeps_result_and_short_cap(self):
        self.generate.return_value = response('{"list_item":"tbody tr","title":"td a","date":""}')
        result = await self.analyze()
        self.assertTrue(result['success'])
        self.assertEqual(result['list_item'], 'tbody tr')
        self.assert_cap(SELECTOR_OUTPUT_TOKENS)
        self.generate.assert_awaited_once()

    async def test_truncated_selector_json_is_not_registered_or_retried(self):
        self.generate.return_value = response('{"list_item":"tr","title":"a"}', 'MAX_TOKENS')
        result = await self.analyze()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'ai_response_review_required')
        self.assertTrue(result['needs_source_review'])
        self.assertNotIn('raw_response', result)
        self.assertNotIn('list_item', result)
        self.generate.assert_awaited_once()
        self.admit.assert_awaited_once()

    async def test_incomplete_or_blocked_selector_never_succeeds(self):
        for value in (response('', 'STOP'), response('{}', 'SAFETY'), types.GenerateContentResponse(),
                      response('{}'), response('[]'), response('{"list_item":"tr","title":null}')):
            with self.subTest(value=value):
                self.generate.return_value = value
                result = await self.analyze()
                self.assertFalse(result['success'])
                self.assertEqual(result['code'], 'ai_response_review_required')

    async def test_vision_list_valid_empty_is_still_a_genuine_zero(self):
        self.generate.return_value = response('[]')
        result = await self.engine._scrape_vision_list(self.page, self.site, privacy_reviewed=True)
        self.assertEqual(result, [])
        self.assert_cap(VISION_OUTPUT_TOKENS)
        self.generate.assert_awaited_once()

    async def test_vision_list_preserves_supported_items(self):
        self.generate.return_value = response('[{"title":"  지원사업 안내  ","date":"2026-09-10"}]')
        result = await self.engine._scrape_vision_list(self.page, self.site, privacy_reviewed=True)
        self.assertEqual(result, [{'title': '지원사업 안내', 'date': '2026-09-10'}])
        self.assert_cap(VISION_OUTPUT_TOKENS)

    async def test_vision_list_truncated_empty_json_is_review_not_zero_posts(self):
        self.generate.return_value = response('[]', 'MAX_TOKENS')
        with self.assertRaises(AiResponseReviewRequired) as caught:
            await self.engine._scrape_vision_list(self.page, self.site, privacy_reviewed=True)
        self.assertEqual(caught.exception.reason, 'output_limit')
        self.generate.assert_awaited_once()
        self.admit.assert_awaited_once()

    async def test_vision_list_malformed_or_missing_output_is_review_not_zero(self):
        for value in (response('', 'STOP'), response('[]', 'SAFETY'), types.GenerateContentResponse(),
                      response('{}'), response('[{"title":""}]'), response('not-json')):
            with self.subTest(value=value):
                self.generate.return_value = value
                with self.assertRaises(AiResponseReviewRequired):
                    await self.engine._scrape_vision_list(self.page, self.site, privacy_reviewed=True)

    async def test_vision_detail_success_is_bounded_and_context_is_closed(self):
        self.generate.return_value = response('교육비 월 5만원 지원')
        result = await self.engine.capture_page_vision(self.site['url'], privacy_reviewed=True)
        self.assertEqual(result, '교육비 월 5만원 지원')
        self.assert_cap(VISION_OUTPUT_TOKENS)
        self.context.close.assert_awaited_once()

    async def test_vision_detail_truncation_does_not_return_empty_or_partial_content(self):
        self.generate.return_value = response('교육비 월', 'MAX_TOKENS')
        with self.assertRaises(AiResponseReviewRequired) as caught:
            await self.engine.capture_page_vision(self.site['url'], privacy_reviewed=True)
        self.assertEqual(caught.exception.reason, 'output_limit')
        self.context.close.assert_awaited_once()
        self.generate.assert_awaited_once()

    async def test_vision_detail_blocked_or_missing_response_closes_context_and_holds(self):
        for value in (response('', 'STOP'), response('소스 내용', 'SAFETY'), types.GenerateContentResponse()):
            with self.subTest(value=value):
                self.context.close.reset_mock()
                self.generate.return_value = value
                with self.assertRaises(AiResponseReviewRequired):
                    await self.engine.capture_page_vision(self.site['url'], privacy_reviewed=True)
                self.context.close.assert_awaited_once()

    async def test_manual_confirmation_is_still_required_before_capture_and_dispatch(self):
        self.assertEqual(await self.engine._scrape_vision_list(self.page, self.site), [])
        self.assertEqual(await self.engine.capture_page_vision(self.site['url']), '')
        self.page.screenshot.assert_not_awaited()
        self.engine.browser.new_context.assert_not_awaited()
        self.generate.assert_not_awaited()
        self.admit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

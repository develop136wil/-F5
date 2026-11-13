import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import ai_classifier
from services import page_coverage as coverage


class TestPageCoverage(unittest.TestCase):
    def test_text_boundary_and_missing_text(self):
        for size, reasons in ((14999, []), (15000, []), (15001, ['page_char_limit']), (0, ['no_text'])):
            with self.subTest(size=size):
                page = ai_classifier._parse_page_html('<p>' + '가' * size + '</p>', 'https://example.org')
                self.assertEqual(page['source_page']['reasons'], reasons)
                self.assertEqual(len(page['text']), min(size, 15000))

    def test_remote_text_cannot_supply_coverage_notes(self):
        actual = coverage.normalize({'reasons': ['fetch_failed', 'fetch_failed', 'secret', {}],
                                     'note': 'private exception', 'status': 'complete'})
        self.assertEqual(actual, coverage.make('fetch_failed'))
        self.assertNotIn('private', json.dumps(actual))
        self.assertEqual(coverage.normalize(None), {})

    def test_html_copy_preserves_page_only_warning(self):
        from services.news_gallery import _copy_text
        text = _copy_text({'title': '안내', 'ai_data': {'source_page': coverage.make('html_byte_limit'),
                                                     'source_input': {'status': 'complete'}}})
        self.assertIn('첨부 링크', text)


class TestPageFetch(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, chunks):
        response = MagicMock()
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        response.headers, response.charset, response.url = {}, 'utf-8', 'https://example.org'
        response.content.read = AsyncMock(side_effect=chunks)
        with patch.dict('os.environ', {'AI_FETCH_MAX_BYTES': '65536'}):
            result = await ai_classifier.fetch_page_content_async(Mock(get=Mock(return_value=response)), response.url)
        response.__aexit__.assert_awaited_once()
        return result

    async def test_exact_byte_limit_is_not_partial(self):
        result = await self.fetch([b' ' * 65528 + b'<p>x</p>', b''])
        self.assertEqual(result['source_page']['status'], 'complete')

    async def test_byte_limit_reports_missing_links_even_when_text_fits(self):
        result = await self.fetch([b' ' * 65528 + b'<p>x</p>', b'<'])
        self.assertEqual(result['text'], 'x')
        self.assertEqual(result['source_page']['reasons'], ['html_byte_limit'])

    async def test_decoding_failure_is_visible(self):
        result = await self.fetch([b'<p>test\xfftext</p>', b''])
        self.assertIn('decode_error', result['source_page']['reasons'])

    async def test_failed_download_is_not_complete(self):
        result = await self.fetch([TimeoutError('private-request')])
        self.assertEqual(result['source_page'], coverage.make('fetch_failed'))
        self.assertNotIn('private-request', json.dumps(result))

    async def test_cancellation_is_not_swallowed_as_a_failure(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.fetch([asyncio.CancelledError()])

    async def test_model_cannot_overwrite_source_page_or_hide_partial_fetch(self):
        source_page = coverage.make('html_byte_limit')
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={
                'is_relevant': True, 'source_page': {'status': 'complete'}})) as api:
            result = await ai_classifier.classify_post_async(object(), '제목', 'url', content='짧은 본문',
                custom_prompt='', strict=True, source_page=source_page)
        self.assertEqual(result['source_input']['status'], 'complete')
        self.assertEqual(result['source_page'], source_page)
        self.assertIn('[Source coverage]', api.call_args.args[1])
        self.assertIn('첨부 링크', coverage.input_note(result))

    async def test_latest_prompt_prevents_unstated_income_and_month_to_day_inference(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={'is_relevant': True})) as api:
            await ai_classifier.classify_post_async(object(), '걷기', 'url', content='운영기간 2026.7.~9.',
                                                    custom_prompt='', strict=True)
        self.assertIn('never default to 소득 무관', api.call_args.args[1])
        self.assertIn('NOT an exact date', api.call_args.args[1])

    async def test_preexisting_balanced_recipe_keeps_its_prompt(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={'is_relevant': True})) as api:
            await ai_classifier.classify_post_async(object(), '걷기', 'url', content='운영기간 2026.7.~9.',
                custom_prompt='', strict=True, evidence_rules=False)
        self.assertNotIn('[Evidence rules', api.call_args.args[1])

    async def test_main_hash_changes_when_only_fetch_coverage_changes(self):
        import history_manager
        from services.scraper_service import ScraperService
        service = ScraperService(headless=True)
        service.ai_rate_limiter = Mock(acquire=AsyncMock())
        base = {'text': '지원 안내 ' * 100, 'images': [], 'attachments': [], 'source_page': coverage.make()}
        partial = copy.deepcopy(base)
        partial['source_page'] = coverage.make('html_byte_limit')
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(side_effect=[base, partial])), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(side_effect=[{'is_relevant': True} for _ in range(2)])):
            results = [await service._process_single_item_ai(None, object(), {'title': '제목', 'link': 'https://example.org'}) for _ in range(2)]
        self.assertNotEqual(results[0]['content_hash'], results[1]['content_hash'])
        self.assertEqual(json.loads(results[1]['ai_data'])['source_page'], partial['source_page'])

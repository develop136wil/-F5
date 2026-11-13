"""Synthetic page/SDK doubles; no browser, network, screenshots or real AI I/O."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from scraper_engine import ScraperEngine
from services import ai_coordinator


class TestVisionPrivacy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.site = dict(name='합성 기관', url='https://example.invalid/list', selectors={})
        self.rows = [dict(site_name='합성 기관', title='공공기관 생활 지원 신청 안내',
                          link='https://example.invalid/post', date='')]
        self.page = MagicMock()
        for name in ('screenshot', 'add_init_script', 'goto', 'close', 'wait_for_selector'):
            setattr(self.page, name, AsyncMock())
        self.page.screenshot.return_value = b'synthetic-image-not-a-person'
        self.page.evaluate = AsyncMock(return_value=self.rows)
        self.context = MagicMock(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.engine = ScraperEngine()
        self.engine.browser = MagicMock(new_context=AsyncMock(return_value=self.context))
        self.generate = AsyncMock(return_value=SimpleNamespace(text='[]'))
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.generate)))
        self.create = patch.object(ai_coordinator, 'create_client', return_value=self.client).start()
        self.slot = patch.object(ai_coordinator, 'acquire_model_slot', new_callable=AsyncMock).start()
        self.rate = patch('scraper_engine._vision_rate_limiter.acquire', new_callable=AsyncMock).start()
        patch('scraper_engine.asyncio.sleep', new_callable=AsyncMock).start()
        patch.dict(os.environ, {'GOOGLE_API_KEY': 'synthetic-key', 'VISION_THRESHOLD':'10'}).start()
        self.addCleanup(patch.stopall)

    async def test_default_and_truthy_values_never_capture_or_dispatch(self):
        self.assertEqual(await self.engine._scrape_vision_list(self.page, self.site), [])
        self.assertEqual(await self.engine.capture_page_vision(self.site['url']), '')
        for value in (False, None, 0, 1, 'true', '1', [True], {'privacy_reviewed':True}):
            with self.subTest(value=value):
                self.assertEqual(await self.engine._scrape_vision_list(self.page, self.site,
                                 privacy_reviewed=value), [])
                self.assertEqual(await self.engine.capture_page_vision(self.site['url'],
                                 privacy_reviewed=value), '')
        self.page.screenshot.assert_not_awaited()
        self.engine.browser.new_context.assert_not_awaited()
        self.create.assert_not_called(); self.slot.assert_not_awaited()
        self.rate.assert_not_awaited(); self.generate.assert_not_awaited()

    async def test_list_true_is_per_call_and_keeps_manual_feature(self):
        self.generate.return_value.text = '[{"title":"지원 안내","date":""}]'
        result = await self.engine._scrape_vision_list(self.page, self.site, privacy_reviewed=True)
        self.assertEqual(result, [{'title':'지원 안내','date':''}])
        self.page.screenshot.assert_awaited_once(); self.slot.assert_awaited_once()
        self.generate.assert_awaited_once()
        self.assertEqual(await self.engine._scrape_vision_list(self.page, self.site), [])
        self.generate.assert_awaited_once()

    async def test_detail_true_is_per_call_and_closes_browser_context(self):
        self.generate.return_value.text = '합성 안내'
        result = await self.engine.capture_page_vision(self.site['url'], privacy_reviewed=True)
        self.assertEqual(result, '합성 안내')
        self.page.screenshot.assert_awaited_once(); self.slot.assert_awaited_once()
        self.generate.assert_awaited_once(); self.context.close.assert_awaited_once()
        self.assertEqual(await self.engine.capture_page_vision(self.site['url']), '')
        self.generate.assert_awaited_once()

    async def test_automatic_list_preserves_links_without_persistent_permission(self):
        self.site.update(privacy_reviewed=True, vision_enabled=True)
        self.engine._enrich_titles = AsyncMock(side_effect=lambda items:items)
        with patch.dict(os.environ, {'VISION_PRIVACY_REVIEWED':'true'}):
            result = await self.engine.scrape_site(self.site, with_status=True)
        self.assertEqual(result, {'state':'finished','items':self.rows,'vision_review_required':True})
        self.page.screenshot.assert_not_awaited(); self.create.assert_not_called()
        self.generate.assert_not_awaited(); self.slot.assert_not_awaited()
        self.assertEqual(await self.engine.scrape_site(self.site), self.rows)

    async def test_empty_unreviewed_screen_is_not_claimed_as_genuine_empty_scope(self):
        self.page.evaluate.return_value = []
        result = await self.engine.scrape_site(self.site, with_status=True)
        self.assertEqual(result, {'state':'finished','items':[],'vision_review_required':True})
        self.page.screenshot.assert_not_awaited(); self.generate.assert_not_awaited()

    async def test_review_flag_does_not_leak_to_next_site_or_sufficient_links(self):
        self.engine._enrich_titles = AsyncMock(side_effect=lambda items:items)
        first = await self.engine.scrape_site(self.site, with_status=True)
        self.assertTrue(first['vision_review_required'])
        self.site['selectors'] = {'list_item':'.post', 'title':'a'}
        second = await self.engine.scrape_site(self.site, with_status=True)
        self.assertFalse(second['vision_review_required'])
        self.site['selectors'] = {}
        self.page.evaluate.return_value = [dict(self.rows[0], link=f'https://example.invalid/{i}') for i in range(10)]
        third = await self.engine.scrape_site(self.site, with_status=True)
        self.assertFalse(third['vision_review_required'])
        self.assertEqual(len(third['items']), 10)
        self.generate.assert_not_awaited()

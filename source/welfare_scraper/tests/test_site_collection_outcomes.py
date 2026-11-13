"""Offline browser doubles: failure status must not change the legacy list API."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeout
from scraper_engine import ScraperEngine


class TestSiteCollectionOutcomes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = [{'title':'노년 생활 지원 신청 안내','link':'https://example.invalid/post/1',
                      'site_name':'기관','date':''}]
        self.page = MagicMock()
        for method in ('add_init_script','goto','wait_for_selector','wait_for_load_state','close'):
            setattr(self.page, method, AsyncMock())
        self.page.evaluate = AsyncMock(return_value=self.rows)
        self.page.query_selector = AsyncMock(return_value=None)
        self.context = MagicMock(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.engine = ScraperEngine()
        self.engine.browser = MagicMock(new_context=AsyncMock(return_value=self.context))
        self.engine._scrape_fallback = AsyncMock(return_value=[])
        self.site = dict(name='기관',url='https://example.invalid/list',max_pages=2,
                         selectors={'list_item':'.item','title':'a'})
        self.sleep = patch('scraper_engine.asyncio.sleep', new_callable=AsyncMock).start()

    def tearDown(self):
        patch.stopall()

    def next_button(self, *, disabled=False):
        button = MagicMock(click=AsyncMock(), is_visible=AsyncMock(return_value=True),
                           get_attribute=AsyncMock(return_value='disabled' if disabled else ''))
        self.site['next_page_selector'] = '.next'
        self.page.query_selector.return_value = button
        return button

    async def collect(self, **kwargs):
        result = await self.engine.scrape_site(self.site, **kwargs)
        self.page.close.assert_awaited()
        self.context.close.assert_awaited()
        return result

    async def test_selector_timeout_is_failed_even_when_fallback_returns_empty(self):
        self.page.wait_for_selector.side_effect = PlaywrightTimeout('private selector detail')
        outcome = await self.collect(with_status=True)
        self.assertEqual(outcome, {'state':'failed','items':[], 'vision_review_required':False})
        self.assertNotIn('private', str(outcome))

    async def test_later_selector_error_preserves_first_page_as_partial(self):
        self.next_button()
        self.page.wait_for_selector.side_effect = [None, RuntimeError('private selector failure')]
        self.assertEqual(await self.collect(with_status=True), {'state':'partial','items':self.rows, 'vision_review_required':False})

    async def test_next_click_failure_preserves_first_page_as_partial(self):
        self.next_button().click.side_effect = RuntimeError('private next error')
        self.assertEqual(await self.collect(with_status=True), {'state':'partial','items':self.rows, 'vision_review_required':False})

    async def test_outer_evaluation_error_keeps_already_read_items(self):
        self.next_button()
        self.page.evaluate.side_effect = [self.rows, RuntimeError('private page error')]
        self.assertEqual(await self.collect(with_status=True), {'state':'partial','items':self.rows, 'vision_review_required':False})

    async def test_initial_navigation_failure_is_failed_not_empty_success(self):
        self.page.goto.side_effect = RuntimeError('private navigation error')
        self.assertEqual(await self.collect(with_status=True), {'state':'failed','items':[], 'vision_review_required':False})

    async def test_no_next_button_is_normal_end_of_detected_scope(self):
        self.assertEqual(await self.collect(with_status=True), {'state':'finished','items':self.rows, 'vision_review_required':False})

    async def test_disabled_next_button_is_normal_end(self):
        button = self.next_button(disabled=True)
        self.assertEqual(await self.collect(with_status=True), {'state':'finished','items':self.rows, 'vision_review_required':False})
        button.click.assert_not_awaited()

    async def test_user_page_limit_is_normal_scope_end(self):
        self.site['max_pages'] = 1
        self.assertEqual(await self.collect(with_status=True), {'state':'finished','items':self.rows, 'vision_review_required':False})
        self.page.query_selector.assert_not_awaited()

    async def test_legacy_list_contract_survives_partial_failure(self):
        self.next_button().click.side_effect = RuntimeError('next error')
        self.assertEqual(await self.collect(), self.rows)

    async def test_valid_empty_result_is_distinct_from_selector_failure(self):
        self.page.evaluate.return_value = []
        self.assertEqual(await self.collect(with_status=True), {'state':'finished','items':[], 'vision_review_required':False})

    async def test_link_fallback_failure_is_explicit_only_in_status_mode(self):
        self.site['selectors'] = {}
        # Restore the real fallback so its nested link error is not swallowed.
        self.engine._scrape_fallback = ScraperEngine._scrape_fallback.__get__(self.engine)
        self.engine._scrape_vision_list = AsyncMock(return_value=[])
        self.page.evaluate.side_effect = RuntimeError('private link error')
        self.assertEqual(await self.collect(with_status=True), {'state':'failed','items':[], 'vision_review_required':False})
        self.engine._scrape_vision_list.assert_not_awaited()
        self.assertEqual(await self.collect(), [])

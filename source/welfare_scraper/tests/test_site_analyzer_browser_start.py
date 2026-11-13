"""First-PC browser startup checks: no browser downloads or live site requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from site_analyzer import SiteAnalyzerService


class SiteAnalyzerBrowserStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = SimpleNamespace(close=AsyncMock(), on=Mock())
        self.browser = SimpleNamespace(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        self.launch = AsyncMock(return_value=self.browser)
        self.driver = SimpleNamespace(chromium=SimpleNamespace(launch=self.launch), stop=AsyncMock())
        self.starter = SimpleNamespace(start=AsyncMock(return_value=self.driver))
        self.service = SiteAnalyzerService()
        self.patches = [
            patch('playwright.async_api.async_playwright', return_value=self.starter),
            patch('scraper_engine.ScraperEngine._install_browser', new_callable=AsyncMock),
        ]
        self.patches[0].start()
        self.install = self.patches[1].start()

    async def asyncTearDown(self):
        await self.service.stop()
        for call in self.context.on.call_args_list:
            if call.args[0] == 'close': call.args[1]()
        for handle in reversed(self.patches):
            handle.stop()

    async def test_edge_success_never_downloads_or_starts_twice(self):
        await self.service.start()
        await self.service.start()
        self.launch.assert_awaited_once_with(headless=True, channel='msedge')
        self.install.assert_not_awaited()
        self.assertTrue(self.service._is_running)
        self.assertEqual(self.browser.new_context.call_args.kwargs['proxy']['bypass'], '<-loopback>')
        self.assertFalse(self.browser.new_context.call_args.kwargs['ignore_https_errors'])

    async def test_missing_edge_uses_existing_chromium_without_download(self):
        self.launch.side_effect = [RuntimeError('Edge missing'), self.browser]
        await self.service.start()
        self.assertEqual([call.kwargs for call in self.launch.await_args_list], [
            {'headless': True, 'channel': 'msedge'}, {'headless': True},
        ])
        self.install.assert_not_awaited()

    async def test_first_pc_downloads_then_launches_chromium_not_edge(self):
        self.launch.side_effect = [RuntimeError('Edge missing'), RuntimeError('Chromium missing'), self.browser]
        await self.service.start()
        self.assertEqual([call.kwargs for call in self.launch.await_args_list], [
            {'headless': True, 'channel': 'msedge'}, {'headless': True}, {'headless': True},
        ])
        self.install.assert_awaited_once_with()
        self.assertTrue(self.service._is_running)

    async def test_download_failure_stops_driver_and_leaves_retryable_state(self):
        self.launch.side_effect = [RuntimeError('Edge missing'), RuntimeError('Chromium missing')]
        self.install.side_effect = RuntimeError('Download unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Download unavailable'):
            await self.service.start()
        self.driver.stop.assert_awaited_once_with()
        self.assertIsNone(self.service.playwright)
        self.assertIsNone(self.service.browser)
        self.assertIsNone(self.service.context)
        self.assertFalse(self.service._is_running)

    async def test_post_install_failure_does_not_loop_or_leave_driver_running(self):
        self.launch.side_effect = [RuntimeError('Edge missing'), RuntimeError('Chromium missing'),
                                   RuntimeError('Chromium launch denied')]
        with self.assertRaisesRegex(RuntimeError, 'Chromium launch denied'):
            await self.service.start()
        self.install.assert_awaited_once_with()
        self.assertEqual(self.launch.await_count, 3)
        self.driver.stop.assert_awaited_once_with()
        self.assertFalse(self.service._is_running)


if __name__ == '__main__':
    unittest.main()

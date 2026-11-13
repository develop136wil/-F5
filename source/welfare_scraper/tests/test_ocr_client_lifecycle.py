"""Owned SDK lifecycle with synthetic clients only; run in the isolated profile."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import ocr_service as ocr


class OcrClientLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.aclose = AsyncMock(side_effect=lambda: self.events.append('async-close'))
        self.close = Mock(side_effect=lambda: self.events.append('sync-close'))
        self.client = SimpleNamespace(aio=SimpleNamespace(aclose=self.aclose), close=self.close)
        self.result = {'is_relevant':True, 'support_detail':'공개 안내'}
        get_patch = patch.object(ocr, 'get_client', return_value=self.client)
        download_patch = patch.object(ocr, 'download_content', new=AsyncMock(return_value=b'public'))
        self.analysis_patch = patch.object(ocr, '_analyze_document_bytes', new=AsyncMock(return_value=self.result))
        self.get, self.download, self.analyze = (item.start() for item in (get_patch, download_patch, self.analysis_patch))
        for item in (get_patch, download_patch, self.analysis_patch):
            self.addCleanup(item.stop)

    async def run_wrapper(self, from_url=False):
        if from_url:
            return await ocr.analyze_image_or_pdf('https://example.invalid/image.png', privacy_reviewed=True)
        return await ocr.analyze_downloaded_document(b'public', privacy_reviewed=True)

    async def test_both_owned_wrappers_close_async_then_sync_after_success(self):
        for from_url in (False, True):
            self.events.clear()
            self.assertIs(await self.run_wrapper(from_url), self.result)
            self.assertEqual(self.events, ['async-close', 'sync-close'])

    async def test_download_failure_still_closes_without_analysis(self):
        self.download.return_value = None
        self.assertEqual(await self.run_wrapper(True), {'error':'Download failed'})
        self.analyze.assert_not_awaited()
        self.assertEqual(self.events, ['async-close', 'sync-close'])

    async def test_download_cancellation_still_closes_without_analysis(self):
        self.download.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_wrapper(True)
        self.analyze.assert_not_awaited()
        self.assertEqual(self.events, ['async-close', 'sync-close'])

    async def test_cleanup_errors_cannot_replace_original_exception(self):
        original = RuntimeError('synthetic analysis failure')
        self.analyze.side_effect = original
        self.aclose.side_effect = ValueError('private cleanup detail')
        self.close.side_effect = RuntimeError('private sync detail')
        with self.assertRaises(RuntimeError) as caught:
            await self.run_wrapper()
        self.assertIs(caught.exception, original)
        self.aclose.assert_awaited_once()
        self.close.assert_called_once()

    async def test_cleanup_errors_and_self_cancellation_cannot_replace_success(self):
        for error in (ValueError('private cleanup detail'), asyncio.CancelledError()):
            self.aclose.side_effect = error
            self.close.side_effect = error
            self.assertIs(await self.run_wrapper(), self.result)

    async def test_slow_close_has_bounded_timeout_and_sync_cleanup_still_runs(self):
        stopped = asyncio.Event()
        async def hanging_close():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.aclose.side_effect = hanging_close
        with patch.object(ocr, '_CLIENT_CLOSE_TIMEOUT', .01):
            self.assertIs(await asyncio.wait_for(self.run_wrapper(), .5), self.result)
        self.assertTrue(stopped.is_set())
        self.close.assert_called_once()

    async def test_cancellation_during_analysis_propagates_after_owned_cleanup(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.analyze.side_effect = pending
        task = asyncio.create_task(self.run_wrapper())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, ['async-close', 'sync-close'])

    async def test_cancellation_during_cleanup_propagates_but_sync_close_runs(self):
        entered = asyncio.Event()
        async def pending_close():
            entered.set()
            await asyncio.Event().wait()
        self.aclose.side_effect = pending_close
        task = asyncio.create_task(self.run_wrapper())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.close.assert_called_once()

    async def test_mock_without_cleanup_support_is_accepted(self):
        self.get.return_value = object()
        self.assertIs(await self.run_wrapper(), self.result)

    async def test_no_consent_never_creates_client_and_missing_key_is_unchanged(self):
        self.assertEqual((await ocr.analyze_downloaded_document(b'public'))['error_code'], ocr.DOCUMENT_REVIEW_CODE)
        self.get.assert_not_called()
        self.get.return_value = None
        self.assertEqual(await self.run_wrapper(), {'error':'API Key missing'})
        self.aclose.assert_not_awaited()
        self.close.assert_not_called()

    async def test_low_level_borrowed_client_is_not_closed(self):
        # Restore just the mocked analysis function, then use the local invalid
        # byte boundary: neither SDK I/O nor wrapper ownership is involved.
        self.analysis_patch.stop()
        result = await ocr._analyze_document_bytes(self.client, b'', privacy_reviewed=True)
        self.assertFalse(result['is_relevant'])
        self.aclose.assert_not_awaited()
        self.close.assert_not_called()

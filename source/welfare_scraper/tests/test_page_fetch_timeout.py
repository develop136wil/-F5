"""Bound safety checks without network, secrets, AI calls or production data."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import ai_classifier
from services.news_site_collector import _needs_browser


URL = 'https://public.example.invalid/start'


def response(status=200, *, url=URL, headers=None):
    value = MagicMock()
    value.__aenter__ = AsyncMock(return_value=value)
    value.__aexit__ = AsyncMock(return_value=False)
    value.status, value.url, value.charset = status, url, 'utf-8'
    value.headers = headers or {}
    value.content.read = AsyncMock(side_effect=[b'<p>Readable public notice</p>', b''])
    return value


class TestPageFetchTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_slow_validation_is_bounded_before_http_and_remains_recoverable(self):
        finished = asyncio.Event()
        async def validate(_url):
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        session = Mock()
        with patch.object(ai_classifier, '_AI_FETCH_TIMEOUT', .01), \
             patch.object(ai_classifier.logger, 'warning') as log:
            page = await asyncio.wait_for(ai_classifier.fetch_page_content_async(
                session, URL, url_validator=validate), timeout=1)
        self.assertTrue(finished.is_set())
        session.get.assert_not_called()
        self.assertEqual(page['fetch_failure_kind'], 'timeout')
        self.assertEqual(page['source_page']['reasons'], ['fetch_failed'])
        self.assertEqual(page['text'], '')
        self.assertTrue(_needs_browser(page))
        self.assertIn('[timeout]', log.call_args.args[0])

    async def test_redirect_validation_is_also_bounded_and_first_response_is_closed(self):
        destination = 'https://next.example.invalid/notice'
        first = response(302, headers={'Location': destination})
        session = Mock(get=Mock(return_value=first))
        visited = []
        async def validate(url):
            visited.append(url)
            if url == destination:
                await asyncio.Event().wait()
            return url
        with patch.object(ai_classifier, '_AI_FETCH_TIMEOUT', .01):
            page = await asyncio.wait_for(ai_classifier.fetch_page_content_async(
                session, URL, url_validator=validate), timeout=1)
        self.assertEqual(visited, [URL, destination])
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        first.__aexit__.assert_awaited_once()
        self.assertEqual(page['fetch_failure_kind'], 'timeout')

    async def test_cancel_during_validation_propagates_without_http_or_failure_warning(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def validate(_url):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        session = Mock()
        with patch.object(ai_classifier.logger, 'warning') as log:
            task = asyncio.create_task(ai_classifier.fetch_page_content_async(
                session, URL, url_validator=validate))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        session.get.assert_not_called()
        log.assert_not_called()

    async def test_private_redirect_is_still_rejected_before_its_request(self):
        destination = 'http://127.0.0.1/private'
        first = response(302, headers={'Location': destination})
        session = Mock(get=Mock(return_value=first))
        validator = AsyncMock(side_effect=[URL, ValueError('private destination')])
        page = await ai_classifier.fetch_page_content_async(session, URL, url_validator=validator)
        self.assertEqual(validator.await_count, 2)
        self.assertEqual(validator.call_args.args[0], destination)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(page['fetch_failure_kind'], 'unsafe_url')
        self.assertFalse(_needs_browser(page))

    async def test_success_validates_each_destination_and_retains_http_timeout(self):
        destination = 'https://next.example.invalid/notice'
        first = response(302, headers={'Location': destination})
        second = response(url=destination)
        session = Mock(get=Mock(side_effect=[first, second]))
        validator = AsyncMock(side_effect=lambda url: url)
        with patch.object(ai_classifier, '_AI_FETCH_TIMEOUT', 7):
            page = await ai_classifier.fetch_page_content_async(session, URL, url_validator=validator)
        self.assertEqual([entry.args[0] for entry in validator.await_args_list], [URL, destination])
        self.assertEqual(page['text'], 'Readable public notice')
        self.assertEqual(page['final_url'], destination)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'].total, 7)
        first.__aexit__.assert_awaited_once()
        second.__aexit__.assert_awaited_once()

    async def test_http_timeout_and_validation_timeout_have_the_same_recovery_kind(self):
        value = response()
        value.__aenter__.side_effect = TimeoutError()
        page = await ai_classifier.fetch_page_content_async(Mock(get=Mock(return_value=value)), URL)
        self.assertEqual(page['fetch_failure_kind'], 'timeout')
        self.assertTrue(_needs_browser(page))

    async def test_stream_timeout_preserves_failure_coverage_and_closes_response(self):
        value = response()
        value.content.read = AsyncMock(side_effect=TimeoutError())
        page = await ai_classifier.fetch_page_content_async(Mock(get=Mock(return_value=value)), URL,
            url_validator=AsyncMock(side_effect=lambda url: url))
        self.assertEqual(page['fetch_failure_kind'], 'timeout')
        self.assertEqual(page['source_page']['reasons'], ['fetch_failed'])
        value.__aexit__.assert_awaited_once()

    async def test_redirect_limit_is_unchanged_and_never_uses_unchecked_autofollow(self):
        value = response(302, headers={'Location': '/again'})
        session = Mock(get=Mock(return_value=value))
        validator = AsyncMock(side_effect=lambda url: url)
        page = await ai_classifier.fetch_page_content_async(session, URL, url_validator=validator)
        self.assertEqual(validator.await_count, 6)
        self.assertEqual(session.get.call_count, 6)
        self.assertEqual(value.__aexit__.await_count, 6)
        self.assertEqual(page['fetch_failure_kind'], 'unsafe_url')
        self.assertTrue(all(not call.kwargs['allow_redirects'] for call in session.get.call_args_list))

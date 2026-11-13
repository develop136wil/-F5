"""No external browser/network calls: validate real production page-reader boundaries."""
import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import bokjiro_collector as collector


def payload(ids, total=18):
    return {'dmCount': {'dsServiceList0Count': total}, 'dsServiceList0': [
        {'WLFARE_INFO_ID': str(identifier), 'WLFARE_INFO_NM': '노년 생활 지원',
         'WLFARE_INFO_OUTL_CN': '생활에 필요한 지원', 'BIZ_CHR_INST_NM': '보건복지부',
         'RETURN_STR': 'BKJR_LFTM_CYC_CD:노년;INTRS_THEMA_CD:생활지원'} for identifier in ids]}


class TestOfficialPageValidation(unittest.TestCase):
    def test_missing_or_invalid_response_is_not_empty_completion(self):
        for value in ({}, {'error':'server'}, {'dmCount':{}, 'dsServiceList0':[]},
                      payload([], total='unknown'), payload([], total=True), payload([], total=-1),
                      {'dmCount':{'dsServiceList0Count':0}},
                      {'dmCount':{'dsServiceList0Count':0},'dsServiceList0':None},
                      {'dmCount':{'dsServiceList0Count':1},'dsServiceList0':[{}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                collector._official_page(value, 1, life_cycles=['노년'])

    def test_valid_final_empty_page_and_order_independent_fingerprint(self):
        empty = collector._official_page(payload([], total=0), 1, life_cycles=['노년'])
        self.assertTrue(empty['complete'])
        a = collector._official_page(payload(['a','b']), 1, life_cycles=['노년'])
        b = collector._official_page(payload(['b','a']), 2, life_cycles=['노년'])
        self.assertFalse(a['complete'])
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        self.assertEqual(a['scanned'], 2)

    def test_unmatched_valid_rows_advance_scan_without_becoming_empty_end(self):
        result = collector._official_page(payload(['a']), 1, life_cycles=['청년'])
        self.assertEqual(result['items'], [])
        self.assertEqual(result['scanned'], 1)
        self.assertFalse(result['complete'])


class TestOfficialPageReader(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_timeout_pacing_and_cleanup(self):
        responses = [SimpleNamespace(ok=True, json=AsyncMock(return_value=value))
                     for value in (payload(['a']), payload([], total=0))]
        page = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(side_effect=responses)), goto=AsyncMock())
        engine = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), browser=SimpleNamespace(
            new_context=AsyncMock(return_value=SimpleNamespace(new_page=AsyncMock(return_value=page)))))
        handle = object()
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch('services.scraper_service._acquire_scrape_lock', return_value=handle), \
             patch('services.scraper_service._release_scrape_lock') as release, \
             patch.object(collector.asyncio, 'sleep', new=AsyncMock()) as pace:
            pages = [item async for item in collector.iter_official_pages(life_cycles=['노년'], start_page=6)]
        self.assertEqual([item['page'] for item in pages], [6, 7])
        sent = page.request.post.call_args_list
        self.assertEqual([json.loads(call.kwargs['data'])['dmSearchParam']['page'] for call in sent], ['6','7'])
        self.assertTrue(all(call.kwargs['timeout'] == 30000 for call in sent))
        pace.assert_awaited_once_with(0.25)
        engine.close.assert_awaited_once()
        release.assert_called_once_with(handle)

    async def test_http_failure_does_not_yield_empty_success_and_cleans_up(self):
        response = SimpleNamespace(ok=False, json=AsyncMock(return_value=payload([], 0)))
        page = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=response)), goto=AsyncMock())
        engine = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), browser=SimpleNamespace(
            new_context=AsyncMock(return_value=SimpleNamespace(new_page=AsyncMock(return_value=page)))))
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch('services.scraper_service._acquire_scrape_lock', return_value=object()), \
             patch('services.scraper_service._release_scrape_lock') as release:
            with self.assertRaises(ValueError):
                _ = [item async for item in collector.iter_official_pages(life_cycles=['노년'])]
        response.json.assert_not_awaited()
        engine.close.assert_awaited_once()
        release.assert_called_once()

    async def test_cancel_during_thread_lock_acquisition_releases_late_handle(self):
        entered, finish = threading.Event(), threading.Event()
        handle = object()
        def acquire():
            entered.set()
            finish.wait(3)
            return handle
        async def read():
            return [page async for page in collector.iter_official_pages(life_cycles=['노년'])]
        with patch('services.scraper_service._acquire_scrape_lock', side_effect=acquire), \
             patch('services.scraper_service._release_scrape_lock') as release, \
             patch('scraper_engine.ScraperEngine') as engine:
            task = asyncio.create_task(read())
            await asyncio.to_thread(entered.wait, 2)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.assert_called_once_with(handle)
            engine.assert_not_called()


if __name__ == '__main__':
    unittest.main()

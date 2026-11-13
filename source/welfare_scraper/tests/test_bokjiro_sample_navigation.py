import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import bokjiro_collector


class _Response:
    async def json(self):
        return {
            'dmCount': {'dsServiceList0Count': 1},
            'dsServiceList0': [{
                'WLFARE_INFO_ID': 'sample-1',
                'WLFARE_INFO_NM': '노년 생활 지원',
                'WLFARE_INFO_OUTL_CN': '생활에 필요한 지원',
                'BIZ_CHR_INST_NM': '보건복지부',
                'RETURN_STR': 'BKJR_LFTM_CYC_CD:노년;INTRS_THEMA_CD:생활지원',
            }],
        }


class _Request:
    async def post(self, *args, **kwargs):
        return _Response()


class _Page:
    def __init__(self):
        self.request = _Request()
        self.goto_kwargs = None

    async def goto(self, url, **kwargs):
        self.goto_kwargs = kwargs


class _Context:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class _Browser:
    def __init__(self, page):
        self.page = page

    async def new_context(self, **kwargs):
        return _Context(self.page)


class _Engine:
    last_page = None

    def __init__(self, headless=True):
        self.page = _Page()
        self.browser = _Browser(self.page)
        _Engine.last_page = self.page

    async def start(self):
        return None

    async def close(self):
        return None


class TestBokjiroSampleNavigation(unittest.IsolatedAsyncioTestCase):
    async def test_sample_does_not_wait_for_network_idle(self):
        with patch('scraper_engine.ScraperEngine', _Engine), patch(
            'services.scraper_service._acquire_scrape_lock', return_value=object()
        ), patch('services.scraper_service._release_scrape_lock'):
            result = await bokjiro_collector.collect_official_sample(
                life_cycles=['노년'], interest_topics=['생활지원'],
                max_pages=1, limit=1,
            )

        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['source_scope'], 'bokjiro_service_search')
        self.assertEqual(result['provider_scope'], 'all')
        self.assertEqual(_Engine.last_page.goto_kwargs['wait_until'], 'domcontentloaded')


if __name__ == '__main__':
    unittest.main()

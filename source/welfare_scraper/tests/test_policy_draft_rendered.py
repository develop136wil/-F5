"""Offline tab traversal: no browser, AI or network."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.policy_draft_source import rendered_draft_source


class RenderedDraftTests(unittest.IsolatedAsyncioTestCase):
    def page(self, *, missing=None):
        page = MagicMock()
        active = {'label': '지원대상'}
        title = MagicMock()
        title.wait_for = AsyncMock()
        title.count = AsyncMock(return_value=1)
        title.inner_text = AsyncMock(return_value='시험 정책')
        body = MagicMock()
        body.count = AsyncMock(return_value=1)
        body.inner_text = AsyncMock(side_effect=lambda: active['label'] + ' 원문 조건')
        tabs = MagicMock()
        def choose(*, has_text):
            label = next(x for x in ('지원대상', '서비스 내용', '신청방법', '추가정보') if has_text.fullmatch(x))
            tab = MagicMock()
            tab.count = AsyncMock(return_value=0 if label == missing else 1)
            tab.get_attribute = AsyncMock(side_effect=lambda _: 'selected' if active['label'] == label else '')
            async def click(**kwargs):
                active['label'] = label
            tab.click = AsyncMock(side_effect=click)
            return tab
        tabs.filter.side_effect = choose
        page.locator.side_effect = lambda selector: {
            '.wlfare-info-nm': title, '.cl-tabfolder-body': body, '.tabfolder-item': tabs}[selector]
        page.wait_for_function = AsyncMock()
        return page

    async def test_all_four_tabs_are_kept(self):
        result = await rendered_draft_source(self.page(),
            'https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=TEST')
        self.assertTrue(result['draft_tabs_complete'])
        for label in ('지원대상', '서비스 내용', '신청방법', '추가정보'):
            self.assertIn(label + ' 원문 조건', result['draft_text'])

    async def test_missing_tab_never_returns_partial_success(self):
        with self.assertRaises(ValueError):
            await rendered_draft_source(self.page(missing='신청방법'),
                'https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId=TEST')

    async def test_other_host_uses_normal_html_parser(self):
        page = MagicMock()
        page.content = AsyncMock(return_value='<main>정책 안내</main>')
        result = await rendered_draft_source(page, 'https://example.org/policy')
        self.assertIn('정책 안내', result['draft_text'])
        page.locator.assert_not_called()

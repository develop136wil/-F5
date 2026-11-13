"""Draft-only public detail boundaries, verified against the named sites.

No keyword excerpts, first-match selection or truncation. A changed/ambiguous
layout falls back to the complete parsed page, which still needs safety checks.
"""
import re
from urllib.parse import urlparse


def draft_body(soup, url):
    host = (urlparse(url).hostname or '').lower()
    selectors = {
        'health.dobong.go.kr': '#mainCont',
        'www.mohw.go.kr': '#contents_body',
        'www.foodvoucher.go.kr': '.agri-food',
    }
    selector = selectors.get(host)
    if selector:
        roots = soup.select(selector)
        if len(roots) == 1 and roots[0].get_text(strip=True):
            return roots[0]
    for tag in ('main', 'article'):
        roots = soup.find_all(tag)
        if len(roots) > 1:
            break
        if len(roots) == 1 and roots[0].get_text(strip=True):
            return roots[0]
    return soup


async def rendered_draft_source(page, url):
    """Read only public content tabs; no login, application or submission."""
    from ai_classifier import _parse_page_html
    host = (urlparse(url).hostname or '').lower()
    if host != 'www.bokjiro.go.kr':
        return _parse_page_html(await page.content(), url)
    if urlparse(url).path != '/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do':
        return _parse_page_html(await page.content(), url)
    title_node = page.locator('.wlfare-info-nm')
    await title_node.wait_for(state='visible', timeout=10000)
    if await title_node.count() != 1:
        raise ValueError('Ambiguous policy title')
    title = (await title_node.inner_text()).strip()
    if not title:
        raise ValueError('Missing policy title')
    parts = [title]
    body = page.locator('.cl-tabfolder-body')
    for label in ('지원대상', '서비스 내용', '신청방법', '추가정보'):
        tab = page.locator('.tabfolder-item').filter(has_text=re.compile('^' + re.escape(label) + '$'))
        if await tab.count() != 1 or await body.count() != 1:
            raise ValueError('Policy detail tabs changed')
        before = (await body.inner_text()).strip()
        selected = 'selected' in (await tab.get_attribute('class') or '').split()
        if not selected:
            await tab.click(timeout=10000)
            await page.wait_for_function(
                "previous => { const node = document.querySelector('.cl-tabfolder-body'); "
                "return node && node.innerText.trim() && node.innerText.trim() !== previous; }",
                arg=before, timeout=10000)
        text = (await body.inner_text()).strip()
        if not text:
            raise ValueError('Empty policy detail tab')
        parts.append(label + '\n' + text)
    text = '\n\n'.join(parts)
    return {'title': title, 'text': text, 'policy_text': text, 'draft_text': text,
            'content_type': 'text/html', 'final_url': url,
            'draft_tabs_complete': True}

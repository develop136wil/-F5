"""Registration fallback: retain addresses without claiming collection is working."""
import asyncio
import re
from urllib.parse import urlparse


def is_likely_homepage_url(value):
    """Advisory only: a query-based board or actual home-page blog is allowed."""
    try:
        url = urlparse(value)
        if url.scheme.lower() not in ('http', 'https') or not url.netloc or url.query:
            return False
        return bool(re.fullmatch(
            r'(?:/?|/(?:index|main|default)(?:\.(?:html?|jsp|do|aspx?|php))?/?)',
            url.path, re.I))
    except (TypeError, ValueError):
        return False


def has_post_evidence(item, page):
    """Conservative sample check; a menu link alone is never sufficient."""
    title = str(item.get('title') or '').strip()
    text = str(page.get('text') or '').strip()
    detail = re.search(r'(?:[?&](?:\w*(?:seq|idx|id|no))=\d+|/(?:view|detail|article|posts?)/)',
                       str(item.get('link') or ''), re.I)
    blocked = any(word in text[:300].lower() for word in
                  ('access denied', 'captcha', '접근이 차단', '로그인이 필요'))
    return bool(detail and len(title) >= 5 and len(text) >= 150 and title in text and not blocked)


async def probe_site(site, *, raise_errors=False):
    async def run():
        from services.scraper_service import ScraperService
        from services.news_attachments import validate_public_attachment_url
        from ai_classifier import fetch_page_content_async
        import aiohttp
        await validate_public_attachment_url(site['url'])
        service = ScraperService(headless=True)
        if raise_errors:
            outcome = await service.test_single_site({**site, 'max_pages': 1}, with_status=True)
            if outcome.get('state') == 'failed':
                raise RuntimeError('사이트 목록을 확인하지 못했습니다.')
            items = outcome['items']
        else:
            items = await service.test_single_site({**site, 'max_pages': 1})
        async with aiohttp.ClientSession() as session:
            for item in items[:3]:
                page = await fetch_page_content_async(
                    session, item['link'], url_validator=validate_public_attachment_url)
                if has_post_evidence(item, page):
                    return True
        return False

    try:
        return await asyncio.wait_for(run(), timeout=60)
    except Exception:
        if raise_errors:
            raise
        return False


async def prepare_site(url, result, probe=probe_site):
    success = (isinstance(result, dict) and result.get('success')
               and result.get('list_item') and result.get('title')
               and not is_likely_homepage_url(url))
    # A home page can yield valid-looking menu selectors. Do not trust those
    # rules; verify an actual post with the same basic fallback used by imports.
    # Home-page blogs remain supported when the probe verifies a post body.
    site = {
        'name': result.get('name') or urlparse(url).netloc if success else urlparse(url).netloc,
        'url': url,
        'base_url': result.get('base_url', '') if success else '',
        'selectors': {key: result.get(key, '') for key in ('list_item', 'title', 'date')} if success else {},
        'max_pages': 3,
        'registration_status': 'rules_ready' if success else 'needs_check',
    }
    if not success and await probe(site):
        site['registration_status'] = 'basic_verified'
    return site

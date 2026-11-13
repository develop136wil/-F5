"""Bounded, read-only public-site attachment probe. No AI or operational DB writes.

Run with an isolated LOCALAPPDATA. --download saves at most two PDF/HWPX files
per explicitly listed page under the workspace's tmp/pdfs for visual QA.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from ai_classifier import fetch_page_content_async
from services import news_attachments


PAGES = [
    ('family-walking', 'https://health.dobong.go.kr/bbs.asp?bmode=D&code=10005006&pcode=12743911'),
    ('child-health', 'https://health.dobong.go.kr/bbs.asp?bmode=D&code=10005006&pcode=12742620'),
]


async def main(download=False):
    output = Path(__file__).resolve().parents[2] / 'tmp' / 'pdfs' / 'attachment-samples'
    failed = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
        for slug, url in PAGES:
            try:
                page = await fetch_page_content_async(session, url)
                items = page.get('attachment_items') or []
                if not page.get('text') and not items:
                    failed = True
                print(json.dumps({'sample': slug, 'page': url, 'body_chars': len(page.get('text') or ''),
                                  'attachments': items, 'images': page.get('images', [])[:2]}, ensure_ascii=False), flush=True)
                if not download:
                    continue
                selected = [item for item in items if item.get('kind') in ('pdf', 'hwpx')][:2]
                for index, item in enumerate(selected):
                    fetched = await news_attachments.download_attachment(session, item)
                    result = await news_attachments._extract_downloaded(fetched)
                    kind = result['kind']
                    if kind not in ('pdf', 'hwpx'):
                        continue
                    output.mkdir(parents=True, exist_ok=True)
                    target = output / f'{slug}-{index}.{kind}'
                    target.write_bytes(fetched['data'])
                    print(json.dumps({'sample': slug, 'source': item['url'], 'path': str(target),
                        'kind': kind, 'bytes': len(fetched['data']), 'status': result['status'],
                        'text_chars': len(result['text']), 'preview': result['text'][:900],
                        'sha256': result['sha256']}, ensure_ascii=False), flush=True)
            except Exception as error:
                failed = True
                print(json.dumps({'sample': slug, 'error': type(error).__name__, 'message': str(error)[:300]}, ensure_ascii=False), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.download)))

"""Opt-in public, read-only page-contract check. No Google/Notion credentials.

Checks the first and one beyond-last page, not full collection completeness.
The app profile and SQLite files are disposable and never touch installed data.
"""
import argparse
import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile


async def check():
    from services.bokjiro_collector import PAGE_SIZE, iter_official_pages
    from services.news_profile_taxonomy import LIFE_CYCLES

    async def one(number):
        async with aclosing(iter_official_pages(
                life_cycles=LIFE_CYCLES, start_page=number, max_pages=1)) as pages:
            rows = [page async for page in pages]
        if len(rows) != 1 or rows[0].get('state') != 'page':
            raise ValueError('Expected one public page response')
        return rows[0]

    first = await one(1)
    beyond_last = (first['total_reported'] + PAGE_SIZE - 1) // PAGE_SIZE + 1
    # Always bound this diagnostic to two API requests, even if metadata changes.
    last = await one(beyond_last)
    assert first['scanned'] <= PAGE_SIZE
    assert last['complete'] and last['scanned'] == 0
    print(json.dumps({'public_read_only': True, 'api_requests': 2,
        'ai_requests': 0, 'notion_writes': 0,
        'first_page': {'scanned': first['scanned'], 'matched': len(first['items']),
                       'total_reported': first['total_reported']},
        'beyond_last_page': beyond_last, 'empty_end_verified': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-public-read', action='store_true')
    parser.add_argument('--isolated-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.confirm_public_read:
        parser.error('This is an external read: explicitly pass --confirm-public-read')
    if args.isolated_child:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            asyncio.run(check())
        finally:
            database = sys.modules.get('database')
            if database is not None:
                database.close_all_connections(strict=True)
        return 0
    with tempfile.TemporaryDirectory(prefix='welfare-public-page-check-') as profile:
        data = Path(profile) / 'welfare-scraper'
        data.mkdir()
        # Prevent legacy workspace DB migration into this disposable profile.
        sqlite3.connect(data / 'welfare.db').close()
        (data / 'config.json').write_text(json.dumps({
            'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
            'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
        }), encoding='utf-8')
        env = dict(os.environ, LOCALAPPDATA=profile, GOOGLE_API_KEY='',
                   NOTION_TOKEN='', NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]',
                   DISCORD_WEBHOOK_URL='', GOOGLE_CHAT_WEBHOOK_URL='')
        return subprocess.run([sys.executable, str(Path(__file__).resolve()),
            '--confirm-public-read', '--isolated-child'], env=env,
            cwd=Path(__file__).resolve().parents[1]).returncode


if __name__ == '__main__':
    sys.exit(main())

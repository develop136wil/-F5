"""Prepare an isolated, credential-free desktop profile for release UI testing."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--profile', required=True)
args = parser.parse_args()
root = Path(args.profile).resolve()
workspace = Path(__file__).resolve().parents[2]
if workspace not in root.parents or root.exists():
    parser.error('Use a new profile directory inside the workspace')
data = root / 'welfare-scraper'
data.mkdir(parents=True)
sqlite3.connect(data / 'welfare.db').close()
(data / 'config.json').write_text(json.dumps({
    'GOOGLE_API_KEY': '', 'NOTION_TOKEN': '', 'NOTION_DATABASE_ID': '', 'NOTION_POLICY_DBS': '[]',
    'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
    'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
}), encoding='utf-8')
os.environ.update(LOCALAPPDATA=str(root), GOOGLE_API_KEY='', NOTION_TOKEN='', NOTION_DATABASE_ID='',
                  NOTION_POLICY_DBS='[]', GOOGLE_CHAT_WEBHOOK_URL='', DISCORD_WEBHOOK_URL='')
sys.path.insert(0, str(workspace / 'welfare_scraper'))
import database
from services import news_gallery, page_coverage
ai = {'item_type': 'event', 'source_page': page_coverage.make('html_byte_limit'),
      'source_input': {'status': 'partial', 'note': '긴 본문의 일부만 분석했습니다. 원문을 확인해주세요.'}}
with database.get_connection() as conn:
    conn.execute('''INSERT INTO welfare_items
                    (title,link,site_name,is_archived,ai_data,support_detail,event_date,event_end_date)
                    VALUES (?,?,?,?,?,?,?,?)''', ('격리 시험용 새소식 · 원문 확인 필요',
        'https://example.invalid/release-fixture', '시험 기관', 0, json.dumps(ai, ensure_ascii=False),
        '실제 정책이 아닌 저장 취소·HTML 출력 검증용 자료입니다.',
        (dt.date.today() + dt.timedelta(days=7)).isoformat(),
        (dt.date.today() + dt.timedelta(days=14)).isoformat()))
    conn.commit()
    news_gallery.store_poster(conn, 'https://example.invalid/release-fixture', '')
database.close_all_connections()
print('Isolated fixture ready; no API keys or operational data copied.')

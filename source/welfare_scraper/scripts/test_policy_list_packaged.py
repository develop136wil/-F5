"""Exercise the packaged list endpoint using synthetic rows in the smoke profile."""
import json
import sqlite3
import sys
import time
import uuid
from contextlib import closing
import test_desktop_executable as smoke

original_gallery_check = smoke.check_news_gallery


def check_list(client, data, html_output=None):
    key = 'offline-list-test'
    source = uuid.uuid4().hex
    client.post('/api/config', json={'items': {'NOTION_POLICY_DBS': json.dumps([
        {'key': key, 'name': 'Offline test', 'data_source_id': source}])}}).raise_for_status()
    fields = [{'field': name, 'old': 'before', 'new': 'after'} for name in
              ['거주지', '생애주기', '추가 자격요건', '대상 소득 수준', '상세 지원 내용']]
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        conn.executemany('''INSERT INTO notion_policies
            (db_key,page_id,title,change_status,pending_diff,source_urls,dead_links)
            VALUES (?,?,?,'detected',?,'[]','[]')''',
            [(key, uuid.uuid4().hex, f'Offline {i}', json.dumps({'fields': fields})) for i in range(125)])
        conn.commit()
    try:
        timings = []
        for _ in range(3):
            start = time.monotonic()
            response = client.get('/api/policy/changes', params={'db_keys': key})
            response.raise_for_status()
            assert len(response.json()['items']) == 125
            timings.append(round(time.monotonic() - start, 3))
        assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
        print(f'PASS: packaged 125-policy/625-field list, seconds={timings}, no AI', flush=True)
    finally:
        client.post('/api/config', json={'items': {'NOTION_POLICY_DBS': '[]'}}).raise_for_status()
        with closing(sqlite3.connect(data / 'welfare.db')) as conn:
            conn.execute('DELETE FROM notion_policies WHERE db_key=?', (key,))
            conn.commit()
    original_gallery_check(client, data, html_output)


if __name__ == '__main__':
    smoke.check_news_gallery = check_list
    smoke.run(sys.argv[1])

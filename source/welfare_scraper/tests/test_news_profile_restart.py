"""Real API/SQLite round trip across independent processes, offline only."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


def worker(phase):
    from unittest.mock import patch
    import socket
    original_connect = socket.socket.connect
    def offline_connect(sock, address):
        # Windows asyncio uses a loopback socketpair for its internal wakeup.
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('External network forbidden')
        return original_connect(sock, address)
    with patch('socket.socket.connect', new=offline_connect):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import database
        from services.news_profiles_api import router
        expected = Path(os.environ['LOCALAPPDATA']) / 'welfare-scraper' / 'welfare.db'
        assert Path(database.DB_FILE).resolve() == expected.resolve()
        database.init_db()
        app = FastAPI()
        app.include_router(router)
        settings = dict(name='도봉구 아동 소식', region='서울특별시 도봉구',
                        life_cycles=['아동'], characteristics=['다문화'],
                        min_age_months=75, max_age_months=None,
                        sources=['sites'], site_ids=['1'])
        try:
            with TestClient(app) as client:
                if phase == 'create':
                    response = client.post('/api/news/profiles', json=settings)
                    assert response.status_code == 200, response.text
                items = client.get('/api/news/profiles').json()['items']
                assert len(items) == 1, items
                url = '/api/news/profiles/' + items[0]['profile_id']
                profile = client.get(url).json()['profile']
                for key, value in settings.items():
                    assert profile[key] == (value if key != 'name' or phase != 'verify' else '수정한 조건'), (key, profile)
                assert profile['bokjiro_mode'] == 'unsupported'
                assert profile['revision'] == (2 if phase == 'verify' else 1)
                if phase == 'edit':
                    settings['name'] = '수정한 조건'
                    response = client.put(url, json={'expected_revision': 1, 'settings': settings})
                    assert response.status_code == 200, response.text
                    stale = client.put(url, json={'expected_revision': 1, 'settings': {**settings, 'name': '오래된 화면'}})
                    assert stale.status_code == 409, stale.text
                with database.get_connection() as conn:
                    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    assert conn.execute('SELECT count(*) FROM news_run_snapshots').fetchone()[0] == 0
        finally:
            database.close_all_connections(strict=True)


class TestNewsProfileRestart(unittest.TestCase):
    def test_create_restart_edit_restart_preserves_settings(self):
        with tempfile.TemporaryDirectory(prefix='welfare-profile-restart-') as root:
            data = Path(root) / 'welfare-scraper'
            data.mkdir()
            # Existing empty DB prevents migration of an operational DB.
            sqlite3.connect(data / 'welfare.db').close()
            (data / 'config.json').write_text(json.dumps({
                'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
                'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
            }), encoding='utf-8')
            env = dict(os.environ, LOCALAPPDATA=root, GOOGLE_API_KEY='', NOTION_TOKEN='',
                       NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]',
                       DISCORD_WEBHOOK_URL='', GOOGLE_CHAT_WEBHOOK_URL='')
            for phase in ('create', 'edit', 'verify'):
                result = subprocess.run([sys.executable, '-m', 'tests.test_news_profile_restart', phase],
                                        cwd=Path(__file__).resolve().parents[1], env=env,
                                        capture_output=True, text=True, encoding='utf-8', timeout=60)
                self.assertEqual(result.returncode, 0, f'{phase}: {result.stdout}\n{result.stderr}')


if __name__ == '__main__':
    worker(sys.argv[1])

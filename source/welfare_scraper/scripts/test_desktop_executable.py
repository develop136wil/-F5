"""Opt-in smoke test of a built backend EXE with a disposable profile and port.

No real API keys/sites/Notion configuration are used. Only processes started here
can be terminated by this script. Run: python scripts/test_desktop_executable.py PATH
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid

import httpx


def check_blank_first_use(client):
    """Packaged first-use guards; all state is disposable and no remote work starts."""
    condition = client.get('/api/news/default-condition')
    condition.raise_for_status()
    assert condition.json()['profile'] is None
    assert condition.json()['legacy_collection_available'] is False
    feed = client.get('/api/news/feed')
    feed.raise_for_status()
    assert feed.json()['counts'] == dict(ready=0, review=0, processing=0, records=0)
    assert feed.json()['configured'] is False
    denied = client.post('/api/scrape', json={})
    assert denied.status_code == 409
    assert '수집 조건을 먼저 저장' in denied.json()['detail']
    policies = client.get('/api/policy/dbs')
    policies.raise_for_status()
    assert policies.json()['dbs'] == []
    # Reject before URL validation, credentials, or a Notion write is attempted.
    unchecked = client.post('/api/policy/draft-from-url/create', json={
        'url': 'https://example.invalid/first-policy', 'db_key': 'unconfigured',
        'fields': {'title': '격리 시험용 초안'}, 'source_review_confirmed': False,
    })
    assert unchecked.status_code == 409
    assert unchecked.json()['detail']['error'] == 'SOURCE_REVIEW_CONFIRMATION_REQUIRED'
    usage = client.get('/api/ai/status').json()
    assert usage['recorded_requests_today'] == 0
    assert usage['daily_request_limit'] is None
    assert usage['local_budget_remaining'] is None
    assert usage['local_budget_exhausted'] is False
    # Old clients/configs cannot restore the removed app daily cap.
    assert client.get('/api/config').json()['AI_DAILY_REQUEST_LIMIT'] is None
    client.post('/api/config', json={'items': {'AI_DAILY_REQUEST_LIMIT': '0'}}).raise_for_status()
    assert client.get('/api/config').json()['AI_DAILY_REQUEST_LIMIT'] is None
    assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
    assert client.get('/api/sites/2147483647/test-status').status_code == 404
    assert client.get('/api/desktop/status').json()['busy'] is False
    print('PASS: packaged blank first-use, missing-condition and unchecked-draft guards, no AI', flush=True)


def check_news_gallery(client, data, html_output=None):
    """Exercise packaged HTML output without credentials or remote poster requests."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from services.news_gallery import store_poster
    from services.page_coverage import make

    link = 'https://example.invalid/packaged-gallery-test'
    title = '격리 시험용 새소식'
    note = '긴 본문의 일부만 분석했습니다. 원문을 확인해주세요.'
    analysis = {'item_type': 'event', 'source_page': make('html_byte_limit'),
                'source_input': {'status': 'partial', 'note': note},
                'source_attachments': {'status': 'review_required', 'note': '첨부파일 미확인',
                    'files': [{'filename': '시험 안내.pdf', 'url': 'https://example.invalid/guide.pdf',
                               'status': 'skipped_limit'}]}}
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        cursor = conn.execute('''INSERT INTO welfare_items
            (title,link,site_name,is_archived,ai_data,support_detail) VALUES (?,?,?,?,?,?)''',
            (title, link, '시험 기관', 0, json.dumps(analysis, ensure_ascii=False),
             '실제 정책이 아닌 출력 시험 자료입니다.'))
        item_id = cursor.lastrowid
        store_poster(conn, link, '')  # Fresh negative cache: no network or browser needed.
    candidates = client.get('/api/notion/candidates')
    candidates.raise_for_status()
    assert candidates.json()['configured'] is False
    item = next(row for row in candidates.json()['items'] if row['id'] == item_id)
    assert item['input_review_required'] is True
    assert '페이지 용량 제한' in item['input_note'] and note in item['input_note']
    assert client.post('/api/news/gallery-html', json={'ids': []}).status_code == 400
    for _ in range(2):
        output = client.post('/api/news/gallery-html', json={'ids': [item_id, item_id]}, timeout=30)
        output.raise_for_status()
        assert output.headers['X-WelfareF5-AI-Calls'] == '0'
        assert output.headers['X-WelfareF5-Poster-Cache-Hits'] == '1'
        assert output.headers['X-WelfareF5-Posters'] == '0'
        assert output.headers['content-type'].startswith('text/html')
        assert 'attachment;' in output.headers['content-disposition']
        assert title in output.text and '원문 확인 필요' in output.text
        assert '페이지 용량 제한' in output.text and note in output.text
        assert 'data-copy=' in output.text and link in output.text
        assert '첨부파일 열기 (1)' in output.text
        assert 'href="https://example.invalid/guide.pdf"' in output.text
        # Prevent accidentally packaging the old backend with the new frontend.
        assert 'max-width:420px' in output.text
        assert '.media.no-image,.media.missing{aspect-ratio:auto;height:64px}' in output.text
        assert 'object-fit:contain' in output.text
        assert 'id="manual-copy"' in output.text
        assert "return document.execCommand('copy')===true" in output.text
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        assert not conn.execute('SELECT notion_page_id FROM welfare_items WHERE id=?',
                                (item_id,)).fetchone()[0]
    if html_output:
        # This is the real packaged HTTP response, containing only synthetic data.
        html_output.parent.mkdir(parents=True, exist_ok=True)
        with html_output.open('xb') as destination:
            destination.write(output.content)
    print('PASS: packaged news warning, HTML/copy payload, repeated poster-cache hit, no Notion write', flush=True)


def check_live_bokjiro_profile(client, data):
    """Opt-in public Bokjiro read test; writes only to the disposable profile DB."""
    created = client.post('/api/news/profiles', json={
        'name': '설치본 복지로 격리 시험',
        'region': '전국',
        'life_cycles': ['노년'],
        'sources': ['bokjiro'],
    })
    created.raise_for_status()
    profile = created.json()
    assert profile['execution_enabled'] is True
    options = client.get(f"/api/news/profiles/{profile['profile_id']}/workflow-options")
    options.raise_for_status()
    assert options.json()['enabled'] is True
    assert options.json()['ai_required'] is False
    started = client.post(f"/api/news/profiles/{profile['profile_id']}/workflows",
                          json={'expected_revision': profile['revision']})
    started.raise_for_status()
    workflow_id, run_id = started.json()['workflow_id'], started.json()['run_id']
    deadline = time.monotonic() + 120
    status = None
    while time.monotonic() < deadline:
        response = client.get(f'/api/news/workflows/{workflow_id}')
        response.raise_for_status()
        status = response.json()
        if status['state'] in ('complete', 'failed', 'waiting', 'cancelled'):
            break
        time.sleep(.5)
    assert status is not None and status['state'] == 'complete', status
    results = client.get(f"/api/news/profiles/{profile['profile_id']}/results",
                         params={'run_id': run_id, 'limit': 100})
    results.raise_for_status()
    items = results.json()['items']
    assert items, 'Bokjiro returned no official service results'
    assert all(item['source_type'] == 'bokjiro_service' for item in items)
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        assert conn.execute('SELECT COUNT(*) FROM news_analysis_jobs WHERE run_id=?',
                            (run_id,)).fetchone()[0] == 0
        raw = conn.execute('SELECT result_json FROM news_official_results WHERE run_id=?',
                           (run_id,)).fetchall()
        assert raw and all(json.loads(row[0])['ai_called'] is False for row in raw)
    print(f'PASS: live packaged Bokjiro profile workflow ({len(items)} results, no AI)', flush=True)


def run(executable, html_output=None, live_bokjiro=False):
    if html_output:
        html_output = Path(html_output).resolve()
        workspace = Path(__file__).resolve().parents[2]
        if workspace not in html_output.parents or html_output.exists():
            raise ValueError('HTML test output must be a new file inside the workspace')
    with tempfile.TemporaryDirectory(prefix='welfare-desktop-smoke-') as profile:
        data = Path(profile) / 'welfare-scraper'
        data.mkdir()
        sqlite3.connect(data / 'welfare.db').close()
        (data / 'config.json').write_text(json.dumps({
            'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
            'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
        }), encoding='utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        token, instance = uuid.uuid4().hex, uuid.uuid4().hex
        env = dict(os.environ, LOCALAPPDATA=profile, PYTHON_DOTENV_DISABLED='1', WELFARE_API_PORT=str(port),
                   WELFARE_DESKTOP_TOKEN=token, WELFARE_DESKTOP_INSTANCE=instance,
                   GOOGLE_API_KEY='', NOTION_TOKEN='', NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]',
                   DISCORD_WEBHOOK_URL='', GOOGLE_CHAT_WEBHOOK_URL='')
        env.pop('WELFARE_DESKTOP_PARENT_PID', None)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        args = [str(Path(executable).resolve())]
        process_logs = []
        def launch(extra=None):
            log_path = Path(profile) / f'backend-{len(process_logs)}.log'
            log_handle = log_path.open('wb')
            process_logs.append((log_path, log_handle))
            return subprocess.Popen(args, env={**env, **(extra or {})}, cwd=profile,
                                    stdout=log_handle, stderr=log_handle,
                                    creationflags=flags)
        def wait_ready(client, process):
            started = time.monotonic()
            deadline = started + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'backend exited before ready: {process.returncode}')
                try:
                    response = client.get('/api/desktop/status')
                    if response.status_code == 200 and response.json()['instance'] == instance:
                        print(f'Packaged backend ready in {time.monotonic() - started:.3f}s (disposable profile)', flush=True)
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            raise TimeoutError('backend startup exceeded 90 seconds')
        process = launch()
        children = [process]
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'X-Welfare-Session': token},
                              trust_env=False, timeout=5) as client:
                wait_ready(client, process)
                assert client.get('/api/health').status_code == 200
                ai_status = client.get('/api/ai/status')
                ai_status.raise_for_status()
                assert ai_status.json()['recorded_requests_today'] == 0
                assert ai_status.json()['usage_timezone'] in ('America/Los_Angeles', 'UTC')
                print('PASS: packaged AI usage metadata (no AI requests)', flush=True)
                check_blank_first_use(client)
                # Verify the packaged backend contains the conservative policy gate,
                # without any credentials, real policy IDs, or remote writes.
                policy_gate = client.post('/api/policy/apply-field-bulk', json={'items': [
                    {'id': 1, 'field': '상세 지원 내용'},
                    {'id': 1, 'field': '추가 자격요건'},
                    {'id': 1, 'field': '알 수 없는 필드'},
                ]})
                policy_gate.raise_for_status()
                gate_result = policy_gate.json()
                assert gate_result['applied'] == 0 and not gate_result['failed']
                assert len(gate_result['skipped']) == 3
                assert all(row['reason'] == 'INDIVIDUAL_REVIEW_REQUIRED'
                           for row in gate_result['skipped'])
                assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
                print('PASS: packaged policy bulk prose/unknown-field guard (no AI)', flush=True)
                check_news_gallery(client, data, html_output)
                if live_bokjiro:
                    check_live_bokjiro_profile(client, data)
                assert client.post('/api/desktop/shutdown', headers={'X-Welfare-Session': 'wrong'}).status_code == 403
                print('PASS: isolated EXE startup, health, unauthorized shutdown refusal', flush=True)
                duplicate = launch()
                children.append(duplicate)
                assert duplicate.wait(timeout=90) != 0
                assert process.poll() is None
                assert client.get('/api/desktop/status').status_code == 200
                print('PASS: occupied port refused without terminating existing backend', flush=True)
                before = client.get('/api/config').json()
                invalid = client.post('/api/config', json={'items': {'TARGET_AGE_MAX_MONTHS': 'invalid'}})
                assert invalid.status_code == 400
                assert client.get('/api/config').json()['revision'] == before['revision']
                saved = client.post('/api/config', json={'items': {'TARGET_AGE_MAX_MONTHS': '108'}})
                saved.raise_for_status()
                assert saved.json()['revision'] != before['revision']
                bad_sites = client.post('/api/sites', json=[
                    {'name': 'valid', 'url': 'https://example.invalid/test'},
                    {'name': 'invalid', 'url': 'not-a-url'}])
                assert bad_sites.status_code == 400
                assert client.get('/api/sites').json() == []
                # A synthetic homepage is intentionally retained for manual board
                # selection, with no outbound page analysis. Registration now runs
                # asynchronously: admission does not imply completion/unlocked DB.
                imported = client.post('/api/sites/import', files={'file': (
                    'sites.CSV', b'name,url,title\nSmoke,https://example.invalid/,\n', 'text/csv')})
                imported.raise_for_status()
                assert imported.json()['count'] == 1
                registration_deadline = time.monotonic() + 90
                while True:
                    registration = client.get('/api/sites/batch/status')
                    registration.raise_for_status()
                    if not registration.json()['is_running']:
                        break
                    if time.monotonic() >= registration_deadline:
                        client.post('/api/sites/batch/cancel').raise_for_status()
                        raise TimeoutError('Test-only site registration did not finish')
                    time.sleep(.25)
                assert registration.json()['success_count'] == 1
                assert registration.json()['review_count'] == 1
                test_site = client.get('/api/sites').json()[0]
                assert test_site['url'] == 'https://example.invalid/'
                assert test_site['test_task']['status'] == 'idle'
                diagnostic = client.get(f"/api/sites/{test_site['id']}/test-status")
                diagnostic.raise_for_status()
                assert diagnostic.json()['status'] == 'idle'
                assert diagnostic.json()['running'] is False
                # Reserved .invalid domain: exercises real frozen job admission,
                # polling and DNS-validation failure without reading a real site.
                started_at = time.monotonic()
                started = client.post(f"/api/sites/{test_site['id']}/test")
                started.raise_for_status()
                assert time.monotonic() - started_at < 3
                assert started.json()['status'] == 'running'
                diagnostic_deadline = time.monotonic() + 90
                while True:
                    diagnostic = client.get(f"/api/sites/{test_site['id']}/test-status")
                    diagnostic.raise_for_status()
                    state = diagnostic.json()
                    assert state['task_id'] == started.json()['task_id']
                    if not state['running']:
                        break
                    if time.monotonic() >= diagnostic_deadline:
                        raise TimeoutError('Reserved-domain diagnostic did not settle')
                    time.sleep(.25)
                assert state['status'] in ('failed', 'timed_out'), state
                assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
                print('PASS: packaged site-test async admission, retained terminal failure, no real site/AI', flush=True)
                assert client.get('/api/desktop/status').json()['busy'] is False
                assert client.get('/api/notion/candidates').status_code == 200
                # Loads the new upload-state module without contacting Notion.
                client.post('/api/notion/upload/allow-retry', json={'ids': []}).raise_for_status()
                taxonomy = client.get('/api/news/profile-taxonomy')
                taxonomy.raise_for_status()
                assert taxonomy.json()['life_cycles'][-1] == '노년'
                assert '한부모·조손' in taxonomy.json()['household_situations']
                retired_sample = client.post('/api/news/bokjiro-sample', json={'settings': {
                    'name': '패키지 검증', 'region': '전국', 'characteristics': ['지원하지 않는 대상']}})
                # Official Bokjiro bulk discovery was intentionally retired from
                # News. A new package must not reactivate that old UI/API path.
                assert retired_sample.status_code == 410
                assert client.get('/api/reports/export', params={'format': 'csv'}).status_code == 200
                print('PASS: packaged config/site/upload, retired Bokjiro news guard and export', flush=True)
                condition = {'name': '재시작 시험 조건', 'region': '서울특별시 도봉구',
                             'life_cycles': ['아동'], 'characteristics': ['다문화'],
                             'min_age_months': 75, 'max_age_months': None, 'sources': ['sites']}
                created = client.post('/api/news/profiles', json=condition)
                created.raise_for_status()
                condition_url = '/api/news/profiles/' + created.json()['profile_id']
                backup = client.get('/api/db/backup')
                backup.raise_for_status()
                restored = client.post('/api/db/restore', files={'file': ('backup.db', backup.content, 'application/octet-stream')}, timeout=30)
                assert restored.status_code == 200, restored.text
                restored.raise_for_status()
                assert restored.json()['status'] == 'success'
                assert client.get('/api/health').status_code == 200
                print('PASS: real EXE SQLite backup, restore, post-restore query', flush=True)
                # Abruptly reset real TCP connections like a closing desktop HTTP
                # client. The backend must still drain and exit below.
                for _ in range(20):
                    with socket.create_connection(('127.0.0.1', port), timeout=5) as reset:
                        reset.sendall((f'GET /api/desktop/status HTTP/1.1\r\nHost: localhost\r\n'
                                       f'X-Welfare-Session: {token}\r\n\r\n').encode())
                        reset.recv(4096)
                        reset.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                         struct.pack('HH' if os.name == 'nt' else 'ii', 1, 0))
                print('PASS: 20 abrupt TCP disconnects issued before graceful shutdown', flush=True)
                client.post('/api/desktop/shutdown').raise_for_status()
                assert process.wait(timeout=30) == 0
                process = launch()
                children.append(process)
                wait_ready(client, process)
                assert str(client.get('/api/config').json()['TARGET_AGE_MAX_MONTHS']) == '108'
                recovered = client.get(condition_url)
                recovered.raise_for_status()
                assert all(recovered.json()['profile'][key] == value for key, value in condition.items())
                assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
                print('PASS: packaged condition survives backup/restore and restart, no AI', flush=True)
                client.post('/api/desktop/shutdown').raise_for_status()
                assert process.wait(timeout=30) == 0
                print('PASS: authenticated graceful exit and restart', flush=True)
                # A held parent handle must turn signaled after this test-only parent exits.
                parent = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=flags)
                children.append(parent)
                process = launch({'WELFARE_DESKTOP_PARENT_PID': str(parent.pid)})
                children.append(process)
                wait_ready(client, process)
                parent.kill()
                parent.communicate(timeout=10)
                assert process.wait(timeout=30) == 0
                print('PASS: lost desktop parent triggers safe backend exit', flush=True)
        except Exception:
            # The disposable profile has no credentials; report only its own child logs.
            for log_path, log_handle in process_logs:
                log_handle.flush()
                print(f'Test-only {log_path.name}:\n' +
                      log_path.read_text(encoding='utf-8', errors='replace')[-6000:], flush=True)
            raise
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    # PyInstaller may have an extraction parent + worker. Target only
                    # this still-running Popen handle's tree, never an image name/port.
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10, creationflags=flags)
                    else:
                        child.kill()
                    child.wait(timeout=10)
            for _, log_handle in process_logs:
                log_handle.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('executable')
    parser.add_argument('--html-output', help='Optional new workspace file for the synthetic HTML response')
    parser.add_argument('--live-bokjiro', action='store_true',
                        help='Run a public Bokjiro read-only workflow in the disposable profile')
    args = parser.parse_args()
    run(args.executable, args.html_output, args.live_bokjiro)

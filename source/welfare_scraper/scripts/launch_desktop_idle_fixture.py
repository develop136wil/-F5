"""Run an installed desktop EXE with disposable data and no external credentials.

Close through the native window. Never terminate unrelated processes.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile


def main():
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', 8421))
    profile = Path(tempfile.mkdtemp(prefix='welfare-installed-ui-'))
    data = profile / 'welfare-scraper'
    data.mkdir()
    sqlite3.connect(data / 'welfare.db').close()
    (data / 'config.json').write_text(json.dumps({
        'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
        'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
    }), encoding='utf-8')
    with (profile / 'stdout.log').open('w', encoding='utf-8') as out, \
         (profile / 'stderr.log').open('w', encoding='utf-8') as err:
        child = subprocess.Popen([str(Path(sys.argv[1]).resolve())],
                                 env=dict(os.environ, LOCALAPPDATA=str(profile), GOOGLE_API_KEY='',
                                          NOTION_TOKEN='', NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]',
                                          DISCORD_WEBHOOK_URL='', GOOGLE_CHAT_WEBHOOK_URL=''),
                                 cwd=profile, stdout=out, stderr=err,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        print(f'Test UI pid={child.pid}; profile={profile}', flush=True)
        try:
            code = child.wait(timeout=600)
        except subprocess.TimeoutExpired:
            print('UI wait expired; close this test window manually. No process killed.', flush=True)
            return 2
        print(f'Test UI exit={code}', flush=True)
        return code


if __name__ == '__main__':
    sys.exit(main())

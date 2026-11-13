"""Launch a desktop EXE with an existing credential-free workspace test profile.

The child is a visible GUI; no console window, process killing, or real config.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable')
    parser.add_argument('profile')
    args = parser.parse_args()
    profile = Path(args.profile).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if workspace not in profile.parents:
        parser.error('Test profile must be inside the workspace')
    config = json.loads((profile / 'welfare-scraper' / 'config.json').read_text(encoding='utf-8'))
    if any(config.get(key) for key in ('GOOGLE_API_KEY', 'NOTION_TOKEN', 'NOTION_DATABASE_ID')):
        parser.error('Test profile must not contain credentials')
    for key in ('SCHEDULE_CONFIG', 'POLICY_WATCH_SCHEDULE'):
        if json.loads(config.get(key, '{}')).get('enabled', True):
            parser.error('Both schedules must be disabled for testing')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', 8421))
    env = dict(os.environ, LOCALAPPDATA=str(profile), APPDATA=str(profile / 'roaming'),
               GOOGLE_API_KEY='', NOTION_TOKEN='', NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]',
               DISCORD_WEBHOOK_URL='', GOOGLE_CHAT_WEBHOOK_URL='')
    with (profile / 'desktop-stdout.log').open('w', encoding='utf-8') as out, \
         (profile / 'desktop-stderr.log').open('w', encoding='utf-8') as err:
        child = subprocess.Popen([str(Path(args.executable).resolve())], cwd=profile, env=env,
                                 stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        print(f'Isolated desktop PID={child.pid}; close its window normally.', flush=True)
        try:
            result = child.wait(timeout=600)
        except subprocess.TimeoutExpired:
            print('Close the test window manually; no process was terminated.', flush=True)
            return 2
        print(f'Desktop exit={result}', flush=True)
        return result


if __name__ == '__main__':
    sys.exit(main())

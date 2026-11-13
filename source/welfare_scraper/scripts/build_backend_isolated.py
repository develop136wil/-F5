"""Build without dependency-inspection imports opening the user's app profile."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--distpath', required=True)
    parser.add_argument('--workpath', required=True)
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='welfare-build-profile-') as profile:
        data = Path(profile) / 'welfare-scraper'
        data.mkdir()
        build_temp = Path(profile) / 'temp'
        build_cache = Path(profile) / 'pyinstaller-cache'
        build_temp.mkdir()
        build_cache.mkdir()
        sqlite3.connect(data / 'welfare.db').close()
        (data / 'config.json').write_text(json.dumps({
            'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
            'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
        }), encoding='utf-8')
        env = {key: value for key, value in os.environ.items()
               if not any(part in key.upper() for part in ('API_KEY', 'TOKEN', 'WEBHOOK', 'SECRET', 'PASSWORD'))
               and not key.upper().startswith('WELFARE_DESKTOP_')}
        env.update(LOCALAPPDATA=profile, APPDATA=profile, TEMP=str(build_temp), TMP=str(build_temp),
                   PYINSTALLER_CONFIG_DIR=str(build_cache), PYTHON_DOTENV_DISABLED='1',
                   PYTHONDONTWRITEBYTECODE='1', GOOGLE_API_KEY='', NOTION_TOKEN='',
                   NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]', DISCORD_WEBHOOK_URL='',
                   GOOGLE_CHAT_WEBHOOK_URL='')
        result = subprocess.run([sys.executable, '-m', 'PyInstaller', 'api-server.spec',
                                 '--noconfirm', '--distpath', args.distpath, '--workpath', args.workpath],
                                cwd=backend, env=env)
        return result.returncode


if __name__ == '__main__':
    sys.exit(main())

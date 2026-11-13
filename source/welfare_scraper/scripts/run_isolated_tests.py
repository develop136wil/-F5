"""Run explicitly selected offline unittest modules with a disposable app profile.

This isolates app files/credentials, not network access. Select offline tests only.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeat', type=int, default=1,
                        help='Repeat the suite in one process to expose leaked state (1-100)')
    parser.add_argument('--measure', action='store_true',
                        help='Report Python allocation, thread and tracked DB connection counts')
    parser.add_argument('modules', nargs='+')
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error('--repeat must be between 1 and 100')
    if any(not name.startswith('tests.test_') or not all(
            ch.isalnum() or ch in '._' for ch in name) for name in args.modules):
        parser.error('Provide dotted tests.test_* module names')
    with tempfile.TemporaryDirectory(prefix='welfare-unit-profile-') as profile:
        data = Path(profile) / 'welfare-scraper'
        data.mkdir()
        sqlite3.connect(data / 'welfare.db').close()
        (data / 'config.json').write_text(json.dumps({
            'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
            'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
        }), encoding='utf-8')
        env = dict(os.environ, LOCALAPPDATA=profile, PYTHON_DOTENV_DISABLED='1', GOOGLE_API_KEY='', NOTION_TOKEN='',
                   NOTION_DATABASE_ID='', NOTION_POLICY_DBS='[]', DISCORD_WEBHOOK_URL='',
                   GOOGLE_CHAT_WEBHOOK_URL='')
        # Close only an already-imported application DB after unittest has
        # finished. Importing it solely for cleanup would create profile files.
        runner = '''import sys, unittest, gc, json, threading, tracemalloc, logging
try:
    repeat = int(sys.argv[1])
    measure = sys.argv[2] == '1'
    if measure:
        tracemalloc.start()
        logging.disable(logging.INFO)
    for cycle in range(repeat):
        print(f'Offline stability cycle {cycle + 1}/{repeat}', flush=True)
        suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[3:])
        result = unittest.TextTestRunner(verbosity=0 if measure else 2).run(suite)
        if not result.wasSuccessful():
            sys.exit(1)
        if measure:
            del suite, result
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            database = sys.modules.get('database')
            print(json.dumps(dict(cycle=cycle + 1, python_bytes=current,
                python_peak_bytes=peak, threads=threading.active_count(),
                tracked_db_connections=len(database._all_conns) if database else None)), flush=True)
finally:
    database = sys.modules.get('database')
    if database is not None:
        database.close_all_connections(strict=True)
'''
        return subprocess.run([sys.executable, '-c', runner, str(args.repeat),
                               '1' if args.measure else '0', *args.modules],
                              cwd=Path(__file__).resolve().parents[1], env=env).returncode


if __name__ == '__main__':
    sys.exit(main())

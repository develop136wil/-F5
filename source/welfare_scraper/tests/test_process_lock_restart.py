"""Only the temporary child started by this test is terminated. No API requests."""
import os
import subprocess
import sys
import tempfile
import unittest


class ProcessLockRestartTests(unittest.TestCase):
    def test_abnormal_exit_releases_ai_and_scraper_locks(self):
        script = '''
import os, sys, sqlite3, logging
logging.disable(logging.CRITICAL)
db_dir = os.path.join(os.environ['LOCALAPPDATA'], 'welfare-scraper')
os.makedirs(db_dir, exist_ok=True)
sqlite3.connect(os.path.join(db_dir, 'welfare.db')).close()
from services import ai_coordinator
from services.scraper_service import _acquire_scrape_lock
root = sys.argv[1]
ai_coordinator._job_lock_path = lambda: os.path.join(root, 'ai.lock')
import utils
utils.get_app_data_dir = lambda: root
a = ai_coordinator._acquire_process_lock()
b = _acquire_scrape_lock()
print('LOCKED' if a is not None and b is not None else 'BUSY', flush=True)
if sys.argv[2] == 'hold':
    sys.stdin.read()
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory)
            backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            args = [sys.executable, '-u', '-c', script, directory]
            child = subprocess.Popen(args + ['hold'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env, cwd=backend_dir, creationflags=flags)
            try:
                # Readiness with a bounded wait rather than blocking readline forever.
                from concurrent.futures import ThreadPoolExecutor
                pool = ThreadPoolExecutor(max_workers=1)
                try:
                    ready = pool.submit(child.stdout.readline)
                    self.assertEqual(ready.result(timeout=30).strip(), 'LOCKED')
                    blocked = subprocess.run(args + ['probe'], capture_output=True,
                                             text=True, env=env, cwd=backend_dir, timeout=30, creationflags=flags)
                    self.assertEqual(blocked.returncode, 0, blocked.stderr)
                    self.assertEqual(blocked.stdout.strip(), 'BUSY')
                finally:
                    child.kill()
                    child.wait(timeout=10)
                    pool.shutdown(wait=True)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
            restarted = subprocess.run(args + ['probe'], capture_output=True,
                                       text=True, env=env, cwd=backend_dir, timeout=30, creationflags=flags)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(restarted.stdout.strip(), 'LOCKED')

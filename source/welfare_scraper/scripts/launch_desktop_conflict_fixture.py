"""Launch the newly built UI against a deliberately occupied port, never user data.

The temporary listener and app are this test's own resources. If 8421 is already
occupied, leave its owner completely untouched and test the same refusal screen.
Close the test app through its UI. The disposable profile is retained for inspection.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

profile = tempfile.mkdtemp(prefix='welfare-ui-conflict-')
data = Path(profile) / 'welfare-scraper'
data.mkdir()
(data / 'config.json').write_text(json.dumps({
    'SCHEDULE_CONFIG': json.dumps({'enabled': False}),
    'POLICY_WATCH_SCHEDULE': json.dumps({'enabled': False}),
}), encoding='utf-8')
listener = socket.socket()
try:
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(('127.0.0.1', 8421))
    listener.listen(1)
except OSError:
    listener.close()
    listener = None
env = dict(os.environ, LOCALAPPDATA=profile)
flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
child = subprocess.Popen([str(Path(sys.argv[1]).resolve())], env=env, cwd=profile,
                         stdout=open(Path(profile) / 'stdout.log', 'w', encoding='utf-8'),
                         stderr=open(Path(profile) / 'stderr.log', 'w', encoding='utf-8'),
                         creationflags=flags)
print(f'Test UI pid={child.pid}; profile={profile}', flush=True)
try:
    code = child.wait(timeout=300)
    print(f'Test UI exit={code}; no foreign process was terminated', flush=True)
finally:
    if listener:
        listener.close()

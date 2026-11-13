"""Read-only package checks; report names/counts, never credential contents.

This checks packaged filenames and recognizable text token patterns. It is not
a guarantee that images or arbitrary binary data contain no private information.
"""
import argparse
import json
from pathlib import Path
import re
from PyInstaller.archive.readers import CArchiveReader


def verify(backend, frontend):
    names = list(CArchiveReader(str(backend)).toc)
    forbidden = {'welfare.db', 'config.json', 'config.json.bak', '.env',
                 'ai_quota_state.json', 'quota_state.json'}
    private_entries = [name for name in names if Path(name.replace('\\', '/')).name.lower() in forbidden
                       or name.lower().endswith(('.db-wal', '.db-shm'))]
    text_files = [path for path in frontend.rglob('*') if path.is_file() and path.suffix.lower() in
                  ('.js', '.json', '.html', '.css', '.txt', '.map')]
    pattern = re.compile(r'AIza[0-9A-Za-z_-]{35}|(?:ntn_|secret_)[A-Za-z0-9]{30,}')
    hits = sum(bool(pattern.search(path.read_text(encoding='utf-8', errors='replace'))) for path in text_files)
    return {'archive_entries': len(names), 'private_data_entries': len(private_entries),
            'frontend_text_files': len(text_files), 'credential_pattern_files': hits}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', type=Path)
    parser.add_argument('frontend', type=Path)
    args = parser.parse_args()
    result = verify(args.backend, args.frontend)
    print(json.dumps(result))
    raise SystemExit(0 if result['archive_entries'] and result['frontend_text_files']
                     and not result['private_data_entries'] and not result['credential_pattern_files'] else 1)

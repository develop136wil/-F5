"""Read-only release data comparison. Never prints settings or stored records."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

FILES = ('config.json', 'config.json.bak', '.env', 'ai_quota_state.json',
         'quota_state.json', 'welfare.db')

def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

def inspect(root):
    with closing(sqlite3.connect((root / 'welfare.db').as_uri() + '?mode=ro', uri=True)) as conn:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
    return integrity, counts

def verify(current, backup):
    """Compare closed-app snapshots; both directories must be quiescent."""
    current, backup = Path(current).resolve(), Path(backup).resolve()
    matches = {}
    for name in FILES:
        left, right = current / name, backup / name
        try:
            if not left.exists() and not right.exists():
                matches[name] = name != 'welfare.db'
            else:
                matches[name] = left.is_file() and right.is_file() and digest(left) == digest(right)
        except OSError:
            matches[name] = False
    inspections = []
    for root in (current, backup):
        try:
            inspections.append(inspect(root))
        except (OSError, sqlite3.Error):
            # Never print DB records, exception text or configuration contents.
            inspections.append(('unreadable', None))
    (integrity, counts), (backup_integrity, old_counts) = inspections
    result = {'files_unchanged': matches, 'integrity': integrity,
              'backup_integrity': backup_integrity,
              'table_counts_unchanged': counts is not None and counts == old_counts,
              'tables': len(counts) if counts is not None else None}
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('current')
    parser.add_argument('backup')
    args = parser.parse_args(argv)
    result = verify(args.current, args.backup)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if (all(result['files_unchanged'].values())
                 and result['integrity'] == result['backup_integrity'] == 'ok'
                 and result['table_counts_unchanged']) else 1


if __name__ == '__main__':
    sys.exit(main())

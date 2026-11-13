"""Read-only installed binary verification; never launches the application.

Only the single Tauri UNK -> NSS bundle marker may differ in the desktop
executable. Backend and icon files must match exactly.
"""
import argparse
import hashlib
import json
from pathlib import Path

RAW_MARKER = b'__TAURI_BUNDLE_TYPE_VAR_UNK'
NSIS_MARKER = b'__TAURI_BUNDLE_TYPE_VAR_NSS'


def desktop_match(installed, built):
    if not installed or not built:
        return 'mismatch'
    if installed == built:
        return 'exact'
    if (built.count(RAW_MARKER) == 1 and built.count(NSIS_MARKER) == 0
            and installed.count(NSIS_MARKER) == 1
            and installed.count(RAW_MARKER) == 0
            and installed == built.replace(RAW_MARKER, NSIS_MARKER, 1)):
        return 'nsis_marker_only'
    return 'mismatch'


def exact_file_match(left, right):
    if left.stat().st_size == 0 or left.stat().st_size != right.stat().st_size:
        return False
    with left.open('rb') as first, right.open('rb') as second:
        return (hashlib.file_digest(first, 'sha256').digest()
                == hashlib.file_digest(second, 'sha256').digest())


def verify(installed_root, desktop, backend, icon):
    checks = {}
    pairs = {
        'desktop': (installed_root / 'welfare-tauri.exe', desktop),
        'backend': (installed_root / 'api-server.exe', backend),
        'icon': (installed_root / 'welfareF5.ico', icon),
    }
    for name, (current, expected) in pairs.items():
        try:
            checks[name] = (desktop_match(current.read_bytes(), expected.read_bytes())
                            if name == 'desktop' else
                            'exact' if exact_file_match(current, expected) else 'mismatch')
        except OSError:
            # Do not expose paths, credentials or binary contents in reports.
            checks[name] = 'unreadable'
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installed_root', type=Path)
    parser.add_argument('desktop', type=Path)
    parser.add_argument('backend', type=Path)
    parser.add_argument('icon', type=Path)
    args = parser.parse_args()
    result = verify(args.installed_root, args.desktop, args.backend, args.icon)
    print(json.dumps(result))
    return 0 if all(value in ('exact', 'nsis_marker_only') for value in result.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())

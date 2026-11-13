"""Disposable files owned by this app; never sweep the shared system Temp folder."""
import os
import re
import stat
import tempfile
import time
from pathlib import Path


_PREFIX = 'WelfareF5_'


def _plain_directory(path):
    info = path.lstat()
    return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not (getattr(info, 'st_file_attributes', 0) & 0x400))


def get_temp_dir(*, create=True):
    """Return a checked app-profile/temp directory, refusing junctions and links."""
    base = Path(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')).absolute()
    profile = base / 'welfare-scraper'
    root = profile / 'temp'
    for path in (profile, root):
        if not path.exists():
            if not create:
                return None
            path.mkdir(parents=False, exist_ok=True)
        if not _plain_directory(path):
            raise OSError('앱 임시 폴더의 안전한 위치를 확인하지 못했습니다.')
    if root.resolve().parent != profile.resolve():
        raise OSError('앱 임시 폴더의 안전한 위치를 확인하지 못했습니다.')
    return root


def create_temp_file(*, prefix='file_', suffix=''):
    """mkstemp-compatible (fd, path), always inside the checked app directory."""
    if (not re.fullmatch(r'[A-Za-z0-9_.-]{0,100}', prefix)
            or not re.fullmatch(r'(?:\.[A-Za-z0-9]{1,12})?', suffix)):
        raise ValueError('임시 파일 이름 형식이 올바르지 않습니다.')
    root = get_temp_dir()
    name_prefix = prefix if prefix.startswith(_PREFIX) else _PREFIX + prefix
    return tempfile.mkstemp(prefix=name_prefix, suffix=suffix, dir=root)


def _owned_file(path, root):
    candidate = Path(path).absolute()
    if (candidate.parent != root.absolute() or not candidate.name.startswith(_PREFIX)
            or candidate.resolve().parent != root.resolve()):
        return False
    info = candidate.lstat()
    return (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not (getattr(info, 'st_file_attributes', 0) & 0x400))


def delete_temp_file(path):
    """Delete only a direct, regular app-created temporary file; otherwise refuse."""
    if not path:
        return False
    try:
        root = get_temp_dir(create=False)
        if root is None or not _owned_file(path, root):
            return False
        os.unlink(path)
        return True
    except (OSError, ValueError):
        return False


def cleanup_stale_temp_files(*, max_age_seconds=3600):
    """Best-effort startup cleanup. Existing shared-Temp files stay untouched."""
    root = get_temp_dir(create=False)
    if root is None:
        return 0
    now, removed = time.time(), 0
    for path in root.iterdir():
        try:
            if (_owned_file(path, root) and now - path.stat().st_mtime > max_age_seconds
                    and delete_temp_file(path)):
                removed += 1
        except (OSError, ValueError):
            continue
    return removed

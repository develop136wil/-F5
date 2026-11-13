"""Versioned news profile repository on a caller-owned SQLite connection.

Use within database.get_connection()/run_db in the app. This module never opens
the operational DB itself. Savepoints preserve the caller's transaction boundary.
No collection API consumes these profiles yet.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
import hashlib
import uuid

from services.news_profile import NewsProfile


class ProfileConflict(ValueError):
    pass


class ProtectedProfile(ValueError):
    pass


@contextmanager
def _atomic(conn):
    name = 'news_' + uuid.uuid4().hex
    conn.execute(f'SAVEPOINT {name}')
    try:
        yield
    except BaseException:
        conn.execute(f'ROLLBACK TO {name}')
        conn.execute(f'RELEASE {name}')
        raise
    else:
        conn.execute(f'RELEASE {name}')


def ensure_schema(conn):
    """Add only new tables; no existing welfare rows/settings are rewritten."""
    with _atomic(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS news_profiles (
            profile_id TEXT PRIMARY KEY,
            current_revision INTEGER NOT NULL CHECK(current_revision > 0),
            protected INTEGER NOT NULL DEFAULT 0 CHECK(protected IN (0,1)),
            archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_profile_versions (
            profile_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision > 0),
            snapshot_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(profile_id, revision),
            FOREIGN KEY(profile_id) REFERENCES news_profiles(profile_id)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_run_snapshots (
            run_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(profile_id, revision)
                REFERENCES news_profile_versions(profile_id, revision)
        )''')


def _decode(row):
    snapshot = json.loads(row[0])
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported news profile schema version')
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    if hashlib.sha256(encoded).hexdigest() != row[1]:
        raise ValueError('News profile snapshot integrity mismatch')
    snapshot.pop('schema_version')
    snapshot.setdefault('interest_topics', [])
    profile = NewsProfile(**snapshot)
    return profile


def get_profile(conn, profile_id, revision=None):
    if revision is None:
        row = conn.execute('''SELECT v.snapshot_json, v.fingerprint, v.revision
            FROM news_profile_versions v JOIN news_profiles p
              ON p.profile_id=v.profile_id AND p.current_revision=v.revision
            WHERE p.profile_id=?''', (profile_id,)).fetchone()
    else:
        row = conn.execute('''SELECT snapshot_json, fingerprint, revision
            FROM news_profile_versions WHERE profile_id=? AND revision=?''',
                           (profile_id, revision)).fetchone()
    if row is None:
        raise KeyError(profile_id)
    result = _decode(row)
    if result.profile_id != profile_id or result.revision != row[2]:
        raise ValueError('News profile identity mismatch')
    return result


def _insert_version(conn, profile):
    conn.execute('''INSERT INTO news_profile_versions
        (profile_id, revision, snapshot_json, fingerprint) VALUES (?,?,?,?)''',
                 (profile.profile_id, profile.revision,
                  json.dumps(profile.snapshot(), ensure_ascii=False), profile.fingerprint))


def create_profile(conn, profile):
    if profile.profile_id == 'legacy-infant':
        raise ProtectedProfile('기본 프로필은 최초 이관 경로에서만 생성할 수 있습니다.')
    if profile.revision != 1:
        raise ValueError('New profiles must start at revision 1')
    with _atomic(conn):
        if conn.execute('SELECT 1 FROM news_profiles WHERE profile_id=?',
                        (profile.profile_id,)).fetchone():
            raise ProfileConflict('이미 존재하는 프로필입니다.')
        conn.execute('INSERT INTO news_profiles(profile_id,current_revision) VALUES (?,1)',
                     (profile.profile_id,))
        _insert_version(conn, profile)
    return profile


def initialize_legacy(conn, profile):
    """Explicit, idempotent first capture. Later supplied settings never replace it."""
    if (profile.profile_id != 'legacy-infant' or profile.revision != 1
            or profile.general_mode != 'legacy_infant_v1'
            or profile.bokjiro_mode != 'legacy_infant_v1'):
        raise ValueError('Expected original legacy profile')
    with _atomic(conn):
        row = conn.execute('SELECT protected FROM news_profiles WHERE profile_id=?',
                           (profile.profile_id,)).fetchone()
        if row:
            if not row[0]:
                raise ProfileConflict('기본 프로필 식별자가 이미 사용 중입니다.')
            return get_profile(conn, profile.profile_id)
        conn.execute('''INSERT INTO news_profiles(profile_id,current_revision,protected)
            VALUES (?,1,1)''', (profile.profile_id,))
        _insert_version(conn, profile)
    return profile


def save_revision(conn, profile, *, expected_revision):
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError('Expected revision must be a positive integer')
    if profile.revision != expected_revision + 1:
        raise ValueError('Revision must advance exactly once')
    with _atomic(conn):
        row = conn.execute('SELECT protected, archived FROM news_profiles WHERE profile_id=?',
                           (profile.profile_id,)).fetchone()
        if row is None:
            raise KeyError(profile.profile_id)
        if row[0]:
            raise ProtectedProfile('기본 프로필을 복제한 뒤 수정해주세요.')
        if row[1]:
            raise ProfileConflict('보관된 프로필은 수정할 수 없습니다.')
        updated = conn.execute('''UPDATE news_profiles SET current_revision=?
            WHERE profile_id=? AND current_revision=?''',
                               (profile.revision, profile.profile_id, expected_revision))
        if updated.rowcount != 1:
            raise ProfileConflict('다른 곳에서 수정되었습니다. 다시 불러와주세요.')
        _insert_version(conn, profile)
    return profile


def clone_profile(conn, source_id, *, new_id, name):
    with _atomic(conn):
        source = get_profile(conn, source_id)
        from services.news_auto_sources import automatic_sources
        clone = automatic_sources(replace(source, profile_id=new_id, revision=1, name=name))
        return create_profile(conn, clone)


def archive_profile(conn, profile_id):
    with _atomic(conn):
        row = conn.execute('SELECT protected FROM news_profiles WHERE profile_id=?',
                           (profile_id,)).fetchone()
        if row is None:
            raise KeyError(profile_id)
        if row[0]:
            raise ProtectedProfile('기본 프로필은 보관할 수 없습니다.')
        conn.execute('UPDATE news_profiles SET archived=1 WHERE profile_id=?', (profile_id,))


def list_profiles(conn, *, include_archived=False):
    rows = conn.execute('''SELECT profile_id, archived FROM news_profiles
        WHERE archived=0 OR ? ORDER BY created_at, profile_id''', (int(include_archived),)).fetchall()
    return [(get_profile(conn, row[0]), bool(row[1])) for row in rows]


def _run_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _run_digest(encoded):
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def prepare_run(conn, profile_id, *, expected_revision, sites):
    """Persist a detached execution input, NOT launch or mark a job running.

    Caller supplies site definitions from a coherent settings read. Missing sites
    are errors, not a fallback to all sites. Credentials must remain outside this
    document. Resume must use get_run_snapshot, never the current profile.
    """
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError('Expected revision must be a positive integer')
    with _atomic(conn):
        row = conn.execute('SELECT current_revision, archived FROM news_profiles WHERE profile_id=?',
                           (profile_id,)).fetchone()
        if row is None:
            raise KeyError(profile_id)
        if row[0] != expected_revision or row[1]:
            raise ProfileConflict('프로필이 변경되었거나 보관되었습니다. 다시 확인해주세요.')
        profile = get_profile(conn, profile_id, expected_revision)
        profile.require_bokjiro_support()
        if not isinstance(sites, (list, tuple)):
            raise ValueError('sites must be a list')
        selected = []
        if 'sites' in profile.sources:
            if not profile.site_ids or len(set(profile.site_ids)) != len(profile.site_ids):
                raise ValueError('사이트 수집에는 중복 없는 사이트 선택이 필요합니다.')
            by_id = {}
            for site in sites:
                if not isinstance(site, dict) or type(site.get('id')) not in (str, int):
                    raise ValueError('Site ID is required')
                site_id = str(site['id'])
                if site_id in by_id:
                    raise ValueError('Duplicate site ID')
                by_id[site_id] = site
            # Allowlist excludes incidental runtime and credential fields.
            fields = ('id', 'name', 'url', 'base_url', 'selectors', 'max_pages')
            for site_id in profile.site_ids:
                if site_id not in by_id:
                    raise ValueError(f'선택한 사이트를 찾을 수 없습니다: {site_id}')
                site = by_id[site_id]
                if not all(isinstance(site.get(k), str) and site[k].strip() for k in ('name', 'url')):
                    raise ValueError('Site name and URL are required')
                if not isinstance(site.get('selectors', {}), dict):
                    raise ValueError('Site selectors must be an object')
                selected.append({k: site[k] for k in fields if k in site})
        elif sites:
            raise ValueError('사이트를 사용하지 않는 실행에는 사이트 설정을 전달하지 마세요.')
        run_id = uuid.uuid4().hex
        from services.news_publication import freeze_window
        value = {'schema_version': 1, 'run_id': run_id,
                 'profile': profile.snapshot(), 'sites': selected,
                 'publication_window': freeze_window(profile)}
        encoded = _run_json(value)  # validates JSON and freezes nested mutable inputs
        conn.execute('''INSERT INTO news_run_snapshots
            (run_id,profile_id,revision,snapshot_json,fingerprint) VALUES (?,?,?,?,?)''',
                     (run_id, profile_id, expected_revision, encoded, _run_digest(encoded)))
    return run_id


def get_run_snapshot(conn, run_id):
    """Read original inputs even after editing/archiving the source profile."""
    row = conn.execute('''SELECT snapshot_json, fingerprint, profile_id, revision
        FROM news_run_snapshots WHERE run_id=?''', (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    encoded, digest, profile_id, revision = row
    if _run_digest(encoded) != digest:
        raise ValueError('Run snapshot integrity mismatch')
    value = json.loads(encoded)
    from services.news_publication import window_from_snapshot
    window_from_snapshot(value)
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported run snapshot schema')
    profile = get_profile(conn, profile_id, revision)
    embedded = value.get('profile')
    if not isinstance(embedded, dict) or embedded.get('schema_version') not in (1, 2):
        raise ValueError('Run snapshot identity mismatch')
    embedded_data = dict(embedded)
    embedded_data.pop('schema_version')
    embedded_data.setdefault('interest_topics', [])
    try:
        embedded_profile = NewsProfile(**embedded_data)
    except (TypeError, ValueError):
        raise ValueError('Run snapshot identity mismatch') from None
    if value.get('run_id') != run_id or embedded_profile != profile:
        raise ValueError('Run snapshot identity mismatch')
    return value

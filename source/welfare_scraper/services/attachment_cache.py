"""Rebuildable, bounded local-document text cache, separate from the user's DB.

No raw files, API keys or plain URLs are retained. Every reuse must be confirmed
by the downloader (conditional GET or matching bytes); this is not an offline cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing


MAX_ENTRIES = 256
MAX_CACHE_BYTES = 32 * 1024 * 1024
RETENTION_SECONDS = 30 * 86400
FULL_REFRESH_SECONDS = 86400


def url_key(url):
    return hashlib.sha256(str(url).encode('utf-8')).hexdigest()


def _valid(record):
    if not isinstance(record, dict):
        return False
    if (record.get('no_store') or not isinstance(record.get('text'), str)
            or not 0 < len(record['text'].strip()) <= 30000 or len(record['text']) > 30000
            or not isinstance(record.get('sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is None):
        return False
    coverage = record.get('coverage', {})
    if not isinstance(coverage, dict):
        return False
    if record.get('status') == 'text_extracted':
        return record.get('kind') in ('pdf', 'hwpx', 'hwp') and not coverage.get('reasons')
    if record.get('status') != 'ocr_text_extracted':
        return False
    reasons = coverage.get('reasons')
    # OCR is reusable only when every selected unit was read without omissions or
    # low-confidence warnings. Retain the method/review marker; cached OCR must not
    # be relabelled as ordinary, verified document text.
    return (record.get('kind') in ('pdf', 'png', 'jpg', 'jpeg', 'webp', 'image')
            and isinstance(reasons, list)
            and all(reason == 'ocr_verification_required' for reason in reasons)
            and type(coverage.get('units_total')) is int and coverage['units_total'] > 0
            and type(coverage.get('units_examined')) is int
            and coverage['units_examined'] == coverage['units_total'])


def _restore_record(row):
    if row is None:
        return None
    result = dict(row)
    raw = result.pop('coverage_json', '{}')
    if not isinstance(raw, str) or len(raw) > 4096:
        return None
    result['coverage'] = json.loads(raw)
    return result


class AttachmentTextCache:
    def __init__(self, path=None):
        self.path = path

    def _connect(self):
        path = self.path
        if path is None:
            # Resolve lazily so isolated test profiles and restored DB locations are respected.
            import database
            path = os.path.join(os.path.dirname(database.DB_FILE), 'attachment-text-cache.sqlite3')
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=1)
        try:
            conn.row_factory = sqlite3.Row
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            conn.execute(f'PRAGMA max_page_count={MAX_CACHE_BYTES // page_size}')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    sha256 TEXT NOT NULL, recipe TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, text TEXT NOT NULL, stored_at REAL NOT NULL,
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(sha256, recipe)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    url_key TEXT PRIMARY KEY, final_key TEXT NOT NULL, recipe TEXT NOT NULL,
                    sha256 TEXT NOT NULL, etag TEXT NOT NULL, last_modified TEXT NOT NULL,
                    filename TEXT NOT NULL, checked_at REAL NOT NULL, full_checked_at REAL NOT NULL
                );
            ''')
            # This cache is independent of the user's data DB. Preserve old text
            # entries while adding coverage required for safe OCR reuse.
            columns = {row[1] for row in conn.execute('PRAGMA table_info(documents)')}
            if 'coverage_json' not in columns:
                try:
                    conn.execute("ALTER TABLE documents ADD COLUMN coverage_json TEXT NOT NULL DEFAULT '{}'")
                except sqlite3.OperationalError:
                    # Another concurrent reader may have completed the migration.
                    if 'coverage_json' not in {row[1] for row in conn.execute('PRAGMA table_info(documents)')}:
                        raise
            return conn
        except BaseException:
            conn.close()
            raise

    def get_resource(self, url, recipe, *, now=None):
        now = time.time() if now is None else now
        try:
            with closing(self._connect()) as conn:
                row = conn.execute('''SELECT r.*, d.kind, d.status, d.text, d.coverage_json FROM resources r
                    JOIN documents d ON d.sha256=r.sha256 AND d.recipe=r.recipe
                    WHERE r.url_key=? AND r.recipe=?''', (url_key(url), recipe)).fetchone()
            result = _restore_record(row)
            if not _valid(result):
                return None
            ages = [now - float(result[key]) for key in ('checked_at', 'full_checked_at')]
            if any(not math.isfinite(age) or age < 0 or age > RETENTION_SECONDS for age in ages):
                return None
            result['conditional_allowed'] = ages[1] < FULL_REFRESH_SECONDS
            return result
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None

    def get_content(self, sha256, recipe, *, now=None):
        now = time.time() if now is None else now
        try:
            with closing(self._connect()) as conn:
                row = conn.execute('SELECT * FROM documents WHERE sha256=? AND recipe=?',
                                   (sha256, recipe)).fetchone()
            result = _restore_record(row)
            if not _valid(result):
                return None
            age = now - float(result['stored_at'])
            return result if math.isfinite(age) and 0 <= age <= RETENTION_SECONDS else None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None

    def store(self, url, recipe, record, *, now=None, full_checked_at=None):
        if not _valid(record):
            return False
        now = time.time() if now is None else now
        full_checked_at = now if full_checked_at is None else full_checked_at
        try:
            coverage_json = json.dumps(record.get('coverage', {}), ensure_ascii=False, allow_nan=False)
            if len(coverage_json) > 4096:
                return False
            with closing(self._connect()) as conn, conn:
                # Reclaim expired/LRU rows within this transaction before inserting, so
                # storage doesn't grow with every URL and no half-written entry is visible.
                # Concurrent writers may have captured slightly later wall-clock values
                # before this transaction got the SQLite lock. Do not erase those rows.
                conn.execute('DELETE FROM resources WHERE checked_at < ?',
                             (now - RETENTION_SECONDS,))
                conn.execute('DELETE FROM resources WHERE url_key=?', (url_key(url),))
                conn.execute('''DELETE FROM resources WHERE url_key IN
                    (SELECT url_key FROM resources ORDER BY checked_at DESC, url_key LIMIT -1 OFFSET ?)''',
                    (MAX_ENTRIES - 1,))
                conn.execute('''DELETE FROM documents WHERE NOT EXISTS
                    (SELECT 1 FROM resources r WHERE r.sha256=documents.sha256 AND r.recipe=documents.recipe)''')
                conn.execute('''INSERT INTO documents
                    (sha256, recipe, kind, status, text, stored_at, coverage_json) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha256, recipe) DO UPDATE SET text=excluded.text,
                    kind=excluded.kind, status=excluded.status, stored_at=excluded.stored_at,
                    coverage_json=excluded.coverage_json''',
                    (record['sha256'], recipe, record['kind'], record['status'], record['text'], now,
                     coverage_json))
                conn.execute('INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
                    url_key(url), url_key(record['final_url']), recipe, record['sha256'],
                    str(record.get('etag') or '')[:1000], str(record.get('last_modified') or '')[:1000],
                    str(record.get('filename') or '')[:300], now, full_checked_at))
            return True
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
            return False

    def invalidate(self, url):
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute('DELETE FROM resources WHERE url_key=?', (url_key(url),))
            return True
        except (OSError, sqlite3.Error):
            return False

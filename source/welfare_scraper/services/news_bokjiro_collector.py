"""Bounded Bokjiro collection for an immutable custom-profile run.

The provider call is read-only and performs no AI work. Results cross the
strict official-result boundary before any local persistence.
"""
import asyncio
from contextlib import aclosing
from urllib.parse import quote

from services import app_lifecycle
from services.bokjiro_collector import DETAIL, iter_official_pages
from services.news_analysis_worker import db_call, _settle_db
from services.news_official_results import register_official_results, ensure_schema as ensure_official_schema
from services.news_profile import NewsProfile
from services.news_profile_store import get_run_snapshot, _atomic


def _profile_from_snapshot(snapshot):
    values = dict(snapshot['profile'])
    values.pop('schema_version', None)
    return NewsProfile(**values)


def _normalize_sample_item(item):
    """Rebuild provider output into the exact server-owned storage contract."""
    if not isinstance(item, dict):
        raise ValueError('Invalid Bokjiro provider item')
    source_id = item.get('service_id')
    if not isinstance(source_id, str):
        raise ValueError('Invalid Bokjiro service ID')
    return {
        'source_type': 'bokjiro_service', 'source_id': source_id,
        'source_key': f'bokjiro:{source_id}',
        'title': item.get('title'), 'summary': item.get('summary', ''),
        'provider': item.get('provider', ''), 'provider_region': '',
        'eligibility_region': '', 'age_range_known': False,
        # Never trust a provider-supplied redirect URL when the official ID is enough.
        'link': DETAIL + quote(source_id, safe=''),
        'life_cycles': item.get('life_cycles', []),
        'household_situations': item.get('household_situations', []),
        'interest_topics': item.get('interest_topics', []),
    }


def _progress(conn, run_id):
    with _atomic(conn):
        ensure_official_schema(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS news_bokjiro_progress (
            run_id TEXT PRIMARY KEY, next_page INTEGER NOT NULL DEFAULT 1,
            complete INTEGER NOT NULL DEFAULT 0, scanned INTEGER NOT NULL DEFAULT 0,
            fetched INTEGER NOT NULL DEFAULT 0, pages_failed INTEGER NOT NULL DEFAULT 0,
            total_reported INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0, unchanged_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_bokjiro_pages (
            run_id TEXT NOT NULL, page INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            PRIMARY KEY(run_id,page), UNIQUE(run_id,fingerprint),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
        conn.execute('INSERT OR IGNORE INTO news_bokjiro_progress(run_id) VALUES (?)', (run_id,))
    row = conn.execute('''SELECT next_page,complete,scanned,fetched,pages_failed,total_reported,
        new_count,unchanged_count,updated_count FROM news_bokjiro_progress WHERE run_id=?''', (run_id,)).fetchone()
    registered = conn.execute('SELECT COUNT(*) FROM news_official_results WHERE run_id=?', (run_id,)).fetchone()[0]
    return dict(next_page=row[0], complete=bool(row[1]), scanned=row[2], fetched=row[3],
        pages_failed=row[4], total_reported=row[5], new=row[6], unchanged=row[7], updated=row[8],
        registered=registered, pages_requested=row[0]-1+row[4])


def _save_page(conn, run_id, page):
    with _atomic(conn):
        current = _progress(conn, run_id)
        if current['complete'] or page['page'] != current['next_page']:
            raise ValueError('Bokjiro cursor changed')
        if page['complete'] and current['scanned'] < page['total_reported']:
            raise ValueError('Bokjiro page ended before the reported total')
        if page['fingerprint']:
            if conn.execute('SELECT 1 FROM news_bokjiro_pages WHERE run_id=? AND fingerprint=?',
                    (run_id, page['fingerprint'])).fetchone():
                raise ValueError('Bokjiro pagination repeated a page')
            conn.execute('INSERT INTO news_bokjiro_pages VALUES (?,?,?)',
                         (run_id, page['page'], page['fingerprint']))
        normalized = [_normalize_sample_item(item) for item in page['items']]
        receipts = register_official_results(conn, run_id, normalized) if normalized else []
        counts = [sum(row['change_state'] == state for row in receipts) for state in ('new', 'unchanged', 'updated')]
        conn.execute('''UPDATE news_bokjiro_progress SET next_page=next_page+1,complete=?,
            scanned=scanned+?,fetched=fetched+?,total_reported=?,new_count=new_count+?,
            unchanged_count=unchanged_count+?,updated_count=updated_count+? WHERE run_id=?''',
            (int(page['complete']), page['scanned'], len(normalized), page['total_reported'], *counts, run_id))
    return _progress(conn, run_id)


def _record_failure(conn, run_id):
    with _atomic(conn):
        _progress(conn, run_id)
        conn.execute('UPDATE news_bokjiro_progress SET pages_failed=pages_failed+1 WHERE run_id=?', (run_id,))
    return _progress(conn, run_id)


async def collect_bokjiro_run(run_id, *, max_pages=5, call_db=db_call,
                              provider=iter_official_pages):
    """Persist each page atomically, with no preview result cap or false completion."""
    if type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise ValueError('Bokjiro pages must be 1..5')
    snapshot = await call_db(get_run_snapshot, run_id)
    profile = _profile_from_snapshot(snapshot)
    filters = profile.require_bokjiro_support()
    if not filters or filters.get('mode') != 'official_tags_v1':
        raise ValueError('Run is not a custom Bokjiro collection')
    if app_lifecycle.shutdown_requested:
        return {'state': 'shutdown', 'source': 'bokjiro', 'registered': 0,
                'ai_called': False}

    progress = await call_db(_progress, run_id)
    summary = lambda state, **extra: dict(progress, state=state, source='bokjiro', ai_called=False, **extra)
    if progress['complete']:
        return summary('finished')
    saved_pages = 0
    try:
        async with aclosing(provider(life_cycles=filters['life_cycles'],
                characteristics=filters['characteristics'], interest_topics=filters['interest_topics'],
                start_page=progress['next_page'], max_pages=max_pages)) as pages:
            async for page in pages:
                if not isinstance(page, dict):
                    raise ValueError('Invalid Bokjiro page')
                if page.get('state') in ('busy', 'shutdown'):
                    return summary(page['state'])
                if (page.get('state') != 'page' or type(page.get('page')) is not int
                        or not isinstance(page.get('items'), list) or len(page['items']) > 1000
                        or type(page.get('complete')) is not bool
                        or type(page.get('scanned')) is not int or page['scanned'] < 0
                        or type(page.get('total_reported')) is not int or page['total_reported'] < 0
                        or not isinstance(page.get('fingerprint'), str)
                        or (not page['complete'] and not page['fingerprint'])
                        or (page['complete'] and (page['items'] or page['scanned']))):
                    raise ValueError('Invalid Bokjiro page contract')
                progress, cancelled = await _settle_db(call_db(_save_page, run_id, page))
                saved_pages += 1
                if cancelled:
                    raise asyncio.CancelledError
                if progress['complete']:
                    return summary('finished', pages_processed=saved_pages)
                if saved_pages >= max_pages:
                    break
    except asyncio.CancelledError:
        raise
    except Exception:
        progress, cancelled = await _settle_db(call_db(_record_failure, run_id))
        if cancelled:
            raise asyncio.CancelledError
        return summary('waiting', pages_processed=saved_pages, stop_reason='bokjiro_page_failed')
    return summary('more_pages' if saved_pages else 'waiting', pages_processed=saved_pages,
                   stop_reason='more_pages' if saved_pages else 'bokjiro_page_failed')

"""Explicit promotion; only unadopted legacy failures may be recovered in place."""
import json
from contextlib import contextmanager
from urllib.parse import urlsplit

from services import news_analysis_store as jobs, news_custom_analysis, page_coverage, news_legacy_recovery
from services.news_profile_store import _atomic, get_run_snapshot
from services.news_source_selection import COMPLETE_SOURCE_CHARS

# SQLite TEXT and the lossless Notion/HTML exporters can preserve this bounded
# prose. Keep narrower scalar limits; transport payload guards still apply.
SOURCE_TEXT_MAX_CHARS = COMPLETE_SOURCE_CHARS
_SOURCE_TEXT_FIELDS = frozenset({'support_detail', 'eligibility_detail'})

# Trusted SQL projection, not a flag accepted from AI or request JSON.
MANUAL_NEWS_SQL = """EXISTS (SELECT 1 FROM news_result_imports ni
    WHERE ni.item_id=welfare_items.id AND ni.link=welfare_items.link
    AND ni.outcome='created')"""


class ImportUnavailable(ValueError):
    """A code-owned explanation, never provider text or a saved AI flag."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def ensure_schema(conn):
    with _atomic(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS news_result_imports (
            job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            item_id INTEGER, link TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('created','existing')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES news_analysis_jobs(job_id),
            FOREIGN KEY(item_id) REFERENCES welfare_items(id) ON DELETE SET NULL)''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_news_result_import_item
            ON news_result_imports(item_id,link,outcome)''')


def _read(conn, profile_id, job_id, *, reviewed_job=None, run_snapshot=None):
    job = reviewed_job if reviewed_job is not None else jobs.get_job(conn, job_id)
    snapshot = run_snapshot if run_snapshot is not None else get_run_snapshot(conn, job['run_id'])
    if snapshot['profile']['profile_id'] != profile_id:
        raise KeyError(job_id)
    if job['state'] != 'complete':
        raise ImportUnavailable('analysis_incomplete')
    if (snapshot['profile']['general_mode'] != 'custom_v1'
            or job['recipe'] not in news_custom_analysis.SUPPORTED_RECIPES):
        raise ImportUnavailable('unsupported_analysis')
    try:
        payload, result = json.loads(job['input_json']), json.loads(job['result_json'])
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid saved JSON') from exc
    if not isinstance(payload, dict) or not isinstance(result, dict):
        raise ValueError('Invalid saved result')
    if result.get('filter_reason') == 'invalid_saved_analysis':
        # A failed local recheck retains the raw analysis for diagnostics only.
        # Neither manual confirmation nor a saved approval can authorize it.
        raise ImportUnavailable('invalid_saved_result')
    analysis = result.get('analysis')
    from services.news_analysis_worker import validate_result
    validate_result(analysis)
    news_custom_analysis.validate_facts(
        analysis, require_interest_topics=news_custom_analysis.recipe_options(job['recipe'])['require_interest_topics'])
    if result.get('target_status') == 'excluded':
        raise ImportUnavailable('excluded')
    if analysis['is_relevant'] is not True:
        raise ImportUnavailable('relevance_unconfirmed')
    if result.get('target_status') not in ('match', 'review'):
        raise ImportUnavailable('invalid_saved_result')
    for key in ('title', 'site_name', 'link'):
        if not isinstance(payload.get(key), str) or not payload[key].strip() or len(payload[key]) > 10000:
            raise ValueError('Missing source identity')
    link = payload['link']
    parsed = urlsplit(link)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
            or any(ord(c) <= 32 for c in link)):
        raise ValueError('Invalid source URL')
    # Keep only known fields. Never copy AI-generated item_type/dates, prompts,
    # provider errors, or Notion state into legacy records.
    facts = {key: analysis[key] for key in ('is_relevant', 'category', 'target', 'residence',
             'life_cycle', 'target_characteristics', 'start_age_months', 'end_age_months', 'evidence_snippet')}
    facts['interest_topics'] = analysis.get('interest_topics', [])
    for key in ('category', 'target', 'support_detail', 'eligibility_detail', 'contact', 'deadline'):
        value = analysis.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError('Invalid text field')
            limit = SOURCE_TEXT_MAX_CHARS if key in _SOURCE_TEXT_FIELDS else 16000
            if len(value) > limit:
                # Refuse an over-limit value; never replace it with a prefix.
                raise ImportUnavailable('import_text_too_long')
            facts[key] = value if key in _SOURCE_TEXT_FIELDS else value.strip()
    page = page_coverage.normalize(analysis.get('source_page'))
    if page:
        facts['source_page'] = page
    for key, statuses in (('source_input', ('partial', 'empty', 'complete')),
                           ('source_attachments', ('review_required', 'complete'))):
        value = analysis.get(key)
        if isinstance(value, dict) and value.get('status') in statuses:
            facts[key] = {'status': value['status'], 'note': str(value.get('note') or '')[:1000]}
    facts['news_import_review'] = result['target_status'] == 'review'
    # Preserve source/summary provenance without replacing the canonical body.
    # Every presentation rechecks these bounded values against that exact body.
    for key in ('source_support_selection', 'news_readable'):
        if isinstance(analysis.get(key), dict):
            facts[key] = json.loads(jobs._encode(analysis[key]))
    # Keep the hold, not the rejected date/diagnostics. Manual confirmation is
    # permission to save a review candidate, not evidence for an active period.
    unverified = analysis.get('unverified_facts')
    if isinstance(unverified, dict) and 'deadline' in unverified:
        from services.news_timing import DEADLINE_UNVERIFIED
        facts['news_timing_review'] = DEADLINE_UNVERIFIED
    fingerprint = jobs._hash({'recipe': 'manual-import-v1', 'profile': snapshot['profile'],
                              'job_id': job_id, 'input': payload, 'result': result})
    return payload, facts, fingerprint, snapshot['profile']['revision']


def eligibility(conn, profile_id, job_id, *, reviewed_job=None, run_snapshot=None):
    """Can this result enter the existing confirmation flow?

    Reuse the import reader, not the AI's accepted/review flags alone. The list
    may supply the job it has just locally revalidated to avoid duplicate work.
    This is not write authority: preview checks existing/ended records and the
    eventual transaction still revalidates the exact fingerprint/confirmation.
    No receipt schema, user record, provider or usage ledger is changed here.
    """
    try:
        _read(conn, profile_id, job_id, reviewed_job=reviewed_job, run_snapshot=run_snapshot)
    except ImportUnavailable as error:
        return {'allowed': False, 'reason': error.reason}
    except (ValueError, KeyError, TypeError):
        return {'allowed': False, 'reason': 'invalid_saved_result'}
    return {'allowed': True, 'reason': 'confirmation_required'}


def _receipt(conn, job_id):
    row = conn.execute('SELECT item_id,outcome FROM news_result_imports WHERE job_id=?', (job_id,)).fetchone()
    return {'status': 'already_imported' if row[1] == 'created' else 'existing', 'item_id': row[0]} if row else None


def _ended(payload, facts):
    from services.notion_service import compute_status
    return compute_status({'title': payload['title'], 'deadline': facts.get('deadline'),
                           'ai_data': facts}) == '종료'


def preview(conn, profile_id, job_id):
    payload, facts, fingerprint, revision = _read(conn, profile_id, job_id)
    receipt = _receipt(conn, job_id)
    existing, recoverable = news_legacy_recovery.destination(conn, payload['link'])
    notes = [page_coverage.input_note(facts)]
    from services.news_timing import timing_review_note
    notes.append(timing_review_note({'deadline': facts.get('deadline'), 'ai_data': facts}))
    attachment = facts.get('source_attachments', {})
    if attachment.get('status') == 'review_required':
        notes.append(attachment.get('note') or '첨부파일을 직접 확인해주세요.')
    if recoverable:
        notes.append('선택해 저장하면 이전 실패 기록을 변경 이력에 보존하고 새 분석 내용을 저장합니다.')
    return {'job_id': job_id, 'title': payload['title'][:500], 'link': payload['link'],
            'revision': revision, 'fingerprint': fingerprint,
            'status': receipt['status'] if receipt else 'existing' if existing and not recoverable else 'ended' if _ended(payload, facts) else 'ready',
            'note': ' '.join(filter(None, notes)), 'ai_called': False}


@contextmanager
def _write_transaction(conn):
    # Reserve the SQLite writer before reading: concurrent callers must not both
    # acquire stale WAL read snapshots and then race to upgrade them to writes.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        with _atomic(conn):
            yield
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise


def import_result(conn, profile_id, job_id, *, fingerprint, confirmed):
    if confirmed is not True:
        raise ValueError('Explicit confirmation required')
    with _write_transaction(conn):
        payload, facts, current, _revision = _read(conn, profile_id, job_id)
        existing, recoverable = news_legacy_recovery.destination(conn, payload['link'])
        if fingerprint != current:
            raise ValueError('Preview changed')
        receipt = _receipt(conn, job_id)
        if receipt:
            return receipt
        if (not existing or recoverable) and _ended(payload, facts):
            raise ValueError('Ended news cannot be added to active candidates')
        if recoverable:
            # The user's selected final save is the only recovery write. Audit
            # preservation and replacement share this transaction and item ID.
            from history_manager.snapshots import _save_snapshot
            summary = '이전 AI 분석 실패 기록 보존 · 선택한 새 소식 분석으로 저장'
            _save_snapshot(conn.cursor(), existing['link'], summary)
            conn.execute('''UPDATE welfare_items SET title=?,site_name=?,category=?,target=?,
                residence=?,life_cycle=?,target_characteristics=?,start_age_months=?,end_age_months=?,
                support_detail=?,eligibility_detail=?,contact=?,deadline=?,ai_data=?,
                age_detail=NULL,target_income=NULL,event_date=NULL,event_end_date=NULL,
                ai_skip_reason=NULL,ai_last_attempt_at=NULL,content_hash=NULL,
                embedding=NULL,embedding_recipe=NULL,last_updated=CURRENT_TIMESTAMP,
                change_log=CASE WHEN COALESCE(change_log,'')='' THEN ? ELSE change_log || char(10) || ? END
                WHERE id=?''',
                (payload['title'],payload['site_name'],facts['category'],facts['target'],
                 ', '.join(facts['residence']),', '.join(facts['life_cycle']),', '.join(facts['target_characteristics']),
                 facts['start_age_months'],facts['end_age_months'],facts.get('support_detail'),
                 facts.get('eligibility_detail'),facts.get('contact'),facts.get('deadline') or None,
                 jobs._encode(facts),summary,summary,existing['id']))
            # 'created' is the trusted adoption receipt for these custom facts,
            # even when their destination is a recovered item rather than a new ID.
            conn.execute('''INSERT INTO news_result_imports(job_id,fingerprint,item_id,link,outcome)
                            VALUES (?,?,?,?, 'created')''',
                         (job_id,current,existing['id'],existing['link']))
            return {'status': 'created', 'item_id': existing['id'], 'recovered': True}
        if existing:
            conn.execute('''INSERT INTO news_result_imports(job_id,fingerprint,item_id,link,outcome)
                            VALUES (?,?,?,?, 'existing')''',
                         (job_id,current,existing['id'],existing['link']))
            return {'status': 'existing', 'item_id': existing['id']}
        cursor = conn.execute('''INSERT INTO welfare_items (
            title,link,site_name,category,target,residence,life_cycle,target_characteristics,
            start_age_months,end_age_months,support_detail,eligibility_detail,contact,deadline,ai_data)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(link) DO NOTHING''',
            (payload['title'], payload['link'], payload['site_name'], facts['category'], facts['target'],
             ', '.join(facts['residence']), ', '.join(facts['life_cycle']), ', '.join(facts['target_characteristics']),
             facts['start_age_months'], facts['end_age_months'], facts.get('support_detail'),
             facts.get('eligibility_detail'), facts.get('contact'), facts.get('deadline') or None,
             jobs._encode(facts)))
        outcome = 'created' if cursor.rowcount == 1 else 'existing'
        item_id = conn.execute('SELECT id FROM welfare_items WHERE link=?', (payload['link'],)).fetchone()[0]
        conn.execute('''INSERT INTO news_result_imports(job_id,fingerprint,item_id,link,outcome)
                        VALUES (?,?,?,?,?)''', (job_id, current, item_id, payload['link'], outcome))
        return {'status': outcome, 'item_id': item_id}

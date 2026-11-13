"""Profile-scoped analysis cache and retry jobs. No legacy welfare row writes.

Caller owns DB connection and executes work through database.run_db. No network
calls here. Input payload must contain the exact title/link/site/content sent to
analysis; recipe must version prompts, model settings and extraction rules.
"""
import json
import hashlib
import os
import uuid
from contextlib import contextmanager

from services.news_profile_store import _atomic, get_run_snapshot, get_profile


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_encode(value).encode('utf-8')).hexdigest()


def ensure_schema(conn):
    with _atomic(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS news_analysis_jobs (
            job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, cache_key TEXT NOT NULL,
            input_json TEXT NOT NULL, model TEXT NOT NULL, recipe TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK(state IN ('queued','running','waiting','complete','failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            claim_token TEXT, retry_after REAL NOT NULL DEFAULT 0,
            reason TEXT, result_json TEXT,
            UNIQUE(run_id, cache_key),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_analysis_cache (
            cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL
        )''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(news_analysis_jobs)')}
        for name, definition in (
            ('cancel_requested', 'INTEGER NOT NULL DEFAULT 0'),
            ('retry_start_attempt', 'INTEGER NOT NULL DEFAULT 0'),
            ('checkpoint_json', 'TEXT'),
        ):
            if name not in columns:
                conn.execute(f'ALTER TABLE news_analysis_jobs ADD COLUMN {name} {definition}')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_job_events (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, event TEXT NOT NULL,
            reason TEXT, attempts INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES news_analysis_jobs(job_id)
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_news_analysis_retry
            ON news_analysis_jobs(state,retry_after)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_run_candidates (
            run_id TEXT NOT NULL, candidate_key TEXT NOT NULL, site_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, route TEXT NOT NULL, job_id TEXT,
            PRIMARY KEY(run_id,candidate_key),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id),
            FOREIGN KEY(job_id) REFERENCES news_analysis_jobs(job_id)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_profile_workflows (
            workflow_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
            stage TEXT NOT NULL,error_code TEXT,summary_json TEXT NOT NULL DEFAULT '{}',
            settings_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
        workflow_columns = {row[1] for row in conn.execute('PRAGMA table_info(news_profile_workflows)')}
        if 'settings_json' not in workflow_columns:
            conn.execute("ALTER TABLE news_profile_workflows ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'")


def _encode_enqueue_input(payload, model, recipe):
    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(k), str) for k in ('title', 'link', 'site_name', 'content')
    ):
        raise ValueError('Exact title/link/site_name/content inputs are required')
    if set(payload) - {'title', 'link', 'site_name', 'content', 'source_page'}:
        raise ValueError('Unexpected analysis input fields')
    if 'source_page' in payload:
        from services.page_coverage import normalize
        if not isinstance(payload['source_page'], dict) or payload['source_page'] != normalize(payload['source_page']):
            raise ValueError('Invalid page coverage')
    if not all(isinstance(v, str) and v.strip() for v in (model, recipe)):
        raise ValueError('Explicit model and analysis recipe versions are required')
    return _encode(payload)


def _insert_encoded_job(conn, run_id, *, payload, encoded, model, recipe, fingerprint):
    key = _hash({'profile': fingerprint, 'input': payload,
                 'model': model, 'recipe': recipe})
    job_id = uuid.uuid4().hex
    conn.execute('''INSERT INTO news_analysis_jobs
        (job_id,run_id,cache_key,input_json,model,recipe) VALUES (?,?,?,?,?,?)
        ON CONFLICT(run_id,cache_key) DO NOTHING''',
                 (job_id, run_id, key, encoded, model, recipe))
    return conn.execute('SELECT job_id FROM news_analysis_jobs WHERE run_id=? AND cache_key=?',
                        (run_id, key)).fetchone()[0]


@contextmanager
def _candidate_batch(conn, run_id):
    """Bind intake writes to one integrity-checked run inside one savepoint.

    Only trusted intake uses this private helper. The callback cannot accept a
    different run/profile/fingerprint, and expires when the savepoint closes.
    No snapshot is cached across batches, connections, or awaited operations.
    """
    from services.news_profile import NewsProfile
    with _atomic(conn):
        snapshot = get_run_snapshot(conn, run_id)
        profile_data = dict(snapshot['profile'])
        profile_data.pop('schema_version')
        profile = NewsProfile(**profile_data)
        fingerprint = profile.fingerprint
        active = True

        def enqueue_candidate(*, payload, model, recipe):
            if not active:
                raise RuntimeError('Candidate batch is no longer active')
            encoded = _encode_enqueue_input(payload, model, recipe)
            return _insert_encoded_job(conn, run_id, payload=payload, encoded=encoded,
                                       model=model, recipe=recipe, fingerprint=fingerprint)

        try:
            yield snapshot, profile, enqueue_candidate
        finally:
            active = False


def enqueue(conn, run_id, *, payload, model, recipe):
    encoded = _encode_enqueue_input(payload, model, recipe)
    with _atomic(conn):
        snapshot = get_run_snapshot(conn, run_id)
        profile_data = snapshot['profile']
        profile = get_profile(conn, profile_data['profile_id'], profile_data['revision'])
        return _insert_encoded_job(conn, run_id, payload=payload, encoded=encoded,
                                   model=model, recipe=recipe, fingerprint=profile.fingerprint)


def _source_review(payload, analysis):
    result = _source_review_status(payload, analysis)
    from services.page_coverage import normalize
    # Only captured local extraction metadata can label the method, not AI output.
    page = normalize(payload.get('source_page')) if isinstance(payload, dict) else {}
    if page.get('methods'):
        result['methods'] = page['methods']
    return result


def _source_review_status(payload, analysis):
    """Acquisition completeness is separate from matching the known target facts."""
    from services.page_coverage import normalize
    content = payload.get('content') if isinstance(payload, dict) else None
    if not isinstance(content, str) or not content.strip():
        return {'status': 'empty', 'reason': 'source_body_missing'}
    analysis = analysis if isinstance(analysis, dict) else {}
    pages = [normalize(payload.get('source_page')), normalize(analysis.get('source_page'))]
    coverage = analysis.get('source_input')
    coverage = coverage if isinstance(coverage, dict) else {}
    attachments = analysis.get('source_attachments')
    attachments = attachments if isinstance(attachments, dict) else {}
    if (any(page.get('status') == 'review_required' for page in pages)
            or coverage.get('status') in ('empty', 'partial')
            or attachments.get('status') == 'review_required'):
        return {'status': 'partial', 'reason': 'source_incomplete'}
    from services.news_analysis_safety import BOUNDED_VERSION, COMPLETE_VERSION
    if analysis.get('validation_version') in (BOUNDED_VERSION, COMPLETE_VERSION):
        from services.news_source_selection import source_review_issue
        scope_issue = source_review_issue(payload, complete_document=analysis.get('validation_version') == COMPLETE_VERSION)
        if scope_issue:
            return ({'status': 'partial', 'reason': 'source_incomplete'}
                    if scope_issue == 'source_page_incomplete'
                    else {'status': 'unknown', 'reason': 'coverage_not_recorded'})
    known_complete = any(page.get('status') == 'complete' for page in pages) or coverage.get('status') == 'complete'
    return {'status': 'complete' if known_complete else 'unknown',
            'reason': '' if known_complete else 'coverage_not_recorded'}


DETAIL_FIELDS = ('category', 'target', 'applicant_detail', 'beneficiary_detail',
                 'applicant_evidence', 'support_detail', 'eligibility_detail', 'contact', 'deadline')
DETAIL_MAX_CHARS = 30000
DETAIL_LIST_ROW_BYTES = 8192
DETAIL_LIST_PAGE_BYTES = 131072


def _project_details(source, *, inline_bytes=None, full_available=True):
    """Whole allowlisted values only: never present a cut-off condition as complete.

    The UTF-8 budget bounds inline text; fixed field counts and character caps
    bound the separate one-record response. No source/prompt/unverified fields.
    """
    details, omissions = {}, {}
    remaining = inline_bytes
    for key in DETAIL_FIELDS:
        value = source.get(key) if isinstance(source, dict) else None
        if not isinstance(value, str) or not value.strip():
            continue
        available = full_available and len(value) <= DETAIL_MAX_CHARS
        fits = len(value) <= DETAIL_MAX_CHARS
        if remaining is not None:
            fits = fits and len(value) <= 4000 and len(value.encode('utf-8')) <= remaining
        if fits:
            details[key] = value
            if remaining is not None:
                remaining -= len(value.encode('utf-8'))
        else:
            omissions[key] = {'length': len(value), 'available': available,
                              'reason': ('deferred' if available else 'display_limit'
                                         if len(value) > DETAIL_MAX_CHARS else 'analysis_unverified')}
    return details, omissions


def _can_expand_details(job, result):
    from services.news_custom_analysis import SUPPORTED_RECIPES
    return (job is not None and job['state'] == 'complete' and job['recipe'] in SUPPORTED_RECIPES
            and isinstance(result, dict) and isinstance(result.get('analysis'), dict)
            and result.get('filter_reason') != 'invalid_saved_analysis')


def get_profile_result_details(conn, profile_id, record_id):
    """One read-only, profile-owned projection; never return the stored raw JSON.

    Job results undergo the same current local grounding as the list/import.
    Official results are reconstructed from their normalized official fields.
    This endpoint confers no import authority and does not call AI or write DB.
    """
    get_profile(conn, profile_id)
    if not isinstance(record_id, str) or not 1 <= len(record_id) <= 500:
        raise ValueError('Invalid result identity')
    if record_id.startswith('job:'):
        job_id = record_id[4:]
        owned = conn.execute('''SELECT j.job_id FROM news_analysis_jobs j
            JOIN news_run_snapshots r ON r.run_id=j.run_id
            WHERE j.job_id=? AND r.profile_id=?''', (job_id, profile_id)).fetchone()
        if not owned:
            raise KeyError(record_id)
        job = get_job(conn, job_id)
        result = json.loads(job['result_json']) if job.get('result_json') else {}
        if not _can_expand_details(job, result):
            raise ValueError('Analysis details are not verified')
        source = result['analysis']
    elif record_id.startswith('official:'):
        row = conn.execute('''SELECT o.run_id,o.payload_json,o.fingerprint
            FROM news_official_results o JOIN news_run_snapshots r ON r.run_id=o.run_id
            WHERE 'official:' || o.run_id || ':' || o.source_key=? AND r.profile_id=?''',
                           (record_id, profile_id)).fetchone()
        if not row:
            raise KeyError(record_id)
        from services import news_official_results as official
        item = json.loads(row[1])
        official._validate_item(item)
        if official._fingerprint(item) != row[2]:
            raise ValueError('Official result changed')
        snapshot = get_run_snapshot(conn, row[0])
        profile = get_profile(conn, profile_id, snapshot['profile']['revision'])
        source = official._result(profile, item)['details']
    else:
        raise KeyError(record_id)
    from services.news_display_projection import analysis_presentation
    display = analysis_presentation(source)
    details, omissions = _project_details(dict(source, support_detail=display['support_detail']))
    return {'record_id': record_id, 'details': details, 'detail_omissions': omissions,
            'presentation_kind': display['presentation_kind'], 'source_note': display['source_note'],
            'ai_called': False}


def list_profile_results(conn, profile_id, *, limit=50, offset=0, run_id=None):
    """Exclude prompts/tokens; only the bounded review_evidence may show source text.

    That separate, read-only source comparison is never an approved detail or an
    import field. All other source bodies remain outside the list projection.
    """
    get_profile(conn, profile_id)
    if run_id is not None and get_run_snapshot(conn, run_id)['profile']['profile_id'] != profile_id:
        raise KeyError(run_id)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid pagination')
    from services.news_official_results import ensure_schema as ensure_official_schema
    ensure_official_schema(conn)
    rows = conn.execute('''SELECT 'official:' || o.run_id || ':' || o.source_key AS record_id,
        NULL AS job_id,r.revision,'complete' AS state,o.payload_json,o.result_json,
        r.rowid AS run_order,0 AS kind_order,o.rowid AS item_order,o.run_id,NULL AS review_reason
        FROM news_official_results o JOIN news_run_snapshots r ON o.run_id=r.run_id
        WHERE r.profile_id=? AND (? IS NULL OR r.run_id=?)
        UNION ALL
        SELECT 'job:' || j.job_id AS record_id, j.job_id,r.revision,
        j.state,j.input_json,j.result_json,r.rowid AS run_order,1 AS kind_order,j.rowid AS item_order,j.run_id,j.reason
        FROM news_analysis_jobs j JOIN news_run_snapshots r ON j.run_id=r.run_id
        WHERE r.profile_id=? AND (? IS NULL OR r.run_id=?)
        UNION ALL
        SELECT 'candidate:' || c.run_id || ':' || c.candidate_key,NULL,r.revision,
        c.route,c.payload_json,NULL,r.rowid,2,c.rowid,c.run_id,NULL
        FROM news_run_candidates c JOIN news_run_snapshots r ON c.run_id=r.run_id
        WHERE r.profile_id=? AND (? IS NULL OR r.run_id=?) AND c.job_id IS NULL
        ORDER BY run_order DESC,kind_order,item_order DESC LIMIT ? OFFSET ?''',
        (profile_id, run_id, run_id, profile_id, run_id, run_id,
         profile_id, run_id, run_id, limit + 1, offset)).fetchall()
    items = []
    snapshots = {}
    detail_budget = DETAIL_LIST_PAGE_BYTES
    for (record_id, job_id, revision, state, raw_input, raw_result, _run_order,
         _kind, _item_order, result_run_id, raw_reason) in rows[:limit]:
        payload = json.loads(raw_input)
        result = json.loads(raw_result) if raw_result else {}
        reviewed_job = None
        if job_id and state == 'complete' and raw_result:
            reviewed_job = get_job(conn, job_id)
            result = json.loads(reviewed_job['result_json'])
        status = result.get('target_status') if state == 'complete' else None
        if state == 'complete' and status not in ('match', 'review', 'excluded'):
            status = 'legacy'
        title = payload.get('title', '')
        analysis = result.get('analysis')
        detail_source = analysis if isinstance(analysis, dict) else result.get('details')
        if reviewed_job is not None and result.get('filter_reason') == 'invalid_saved_analysis':
            # A failed recheck may retain the old analysis for diagnostics. It
            # is not display authority, even when its text fits inline.
            detail_source = None
        # Only complete approved-field values fit inline; deferred text is fetched
        # one record at a time. The separate source comparison is never merged.
        from services.news_display_projection import analysis_presentation
        display = analysis_presentation(detail_source)
        if isinstance(detail_source, dict):
            detail_source = dict(detail_source, support_detail=display['support_detail'])
        details, detail_omissions = _project_details(detail_source,
            inline_bytes=min(DETAIL_LIST_ROW_BYTES, detail_budget),
            full_available=not job_id or _can_expand_details(reviewed_job, result))
        detail_budget -= sum(len(value.encode('utf-8')) for value in details.values())
        from urllib.parse import urlsplit
        link = payload.get('link', '')
        try:
            parsed = urlsplit(link) if isinstance(link, str) else None
            if not parsed or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or len(link) > 10000:
                link = ''
        except ValueError:
            link = ''
        from services.page_coverage import input_note as source_input_note
        input_note = (result.get('input_note') if isinstance(result.get('input_note'), str)
                      else source_input_note(analysis))
        if isinstance(analysis, dict) and isinstance(analysis.get('grounding_note'), str) and analysis['grounding_note']:
            input_note = analysis['grounding_note'] + (' ' + input_note if input_note else '')
        import_eligibility = {'allowed': False, 'reason': 'analysis_incomplete' if job_id else 'not_analysis_result'}
        if reviewed_job is not None:
            from services.news_result_import import eligibility
            if result_run_id not in snapshots:
                snapshots[result_run_id] = get_run_snapshot(conn, result_run_id)
            import_eligibility = eligibility(conn, profile_id, job_id, reviewed_job=reviewed_job,
                                            run_snapshot=snapshots[result_run_id])
        # Only current, locally rechecked results can generate review-only source
        # passages. Never merge these into analysis/details/imported user fields.
        review_evidence = None
        if reviewed_job is not None and status == 'review':
            from services.news_review_evidence import build_review_evidence
            review_evidence = build_review_evidence(analysis, payload.get('content', ''))
        items.append(dict(record_id=record_id, job_id=job_id, revision=revision, state=state, run_id=result_run_id,
                          review_reason=(raw_reason if state == 'failed' and raw_reason in (
                              'ai_input_review_required', 'ai_input_size_review_required',
                              'ai_response_review_required', 'ai_source_review_required') else ''),
                          target_status=status, title=title[:500] if isinstance(title, str) else '',
                          input_note=input_note[:1000], link=link, details=details,
                          presentation_kind=display['presentation_kind'], source_note=display['source_note'],
                          detail_omissions=detail_omissions,
                          import_eligibility=import_eligibility,
                          source_review=(_source_review(payload, analysis) if job_id else
                                         {'status': 'not_applicable', 'reason': ''}),
                          review_evidence=review_evidence,
                          source_type=('bokjiro_service' if result.get('source_type') == 'bokjiro_service' else 'site_analysis'),
                          change_state=(result.get('change_state') if result.get('change_state') in ('new', 'unchanged', 'updated') else None)))
    return {'items': items, 'has_more': len(rows) > limit, 'limit': limit, 'offset': offset}


def get_job(conn, job_id):
    cursor = conn.execute('SELECT * FROM news_analysis_jobs WHERE job_id=?', (job_id,))
    row = cursor.fetchone()
    if row is None:
        raise KeyError(job_id)
    job = dict(zip((d[0] for d in cursor.description), row))
    if job['state'] == 'complete' and job.get('result_json'):
        job['result_json'] = _encode(_review_saved_result(conn, job, json.loads(job['result_json'])))
    return job


def _review_saved_result(conn, job, result):
    """Recheck old results locally; never overwrite saved user items or profiles."""
    from services import news_custom_analysis
    if job['recipe'] not in news_custom_analysis.SUPPORTED_RECIPES:
        return result
    from services.news_analysis_context import NewsAnalysisContext
    from services.news_analysis_safety import assess
    facts = result.get('analysis') if isinstance(result, dict) else None
    if not isinstance(facts, dict):
        return result
    profile = NewsAnalysisContext.from_run(conn, job['run_id']).profile
    payload = json.loads(job['input_json'])
    try:
        from services.news_source_selection import eligible_payload
        options = news_custom_analysis.recipe_options(job['recipe'])
        reviewed = assess(profile, facts, payload['content'], require_source_selection=(
            options['source_unit_rules']
            and eligible_payload(payload, extended=options['whole_document_rules'], complete_document=options['complete_document_rules'])),
            whole_document_rules=options['whole_document_rules'],
            complete_document_rules=options['complete_document_rules'],
            source_payload=payload if options['whole_document_rules'] else None)
    except (ValueError, KeyError, TypeError):
        return dict(result, accepted=False, review_required=True, target_status='review',
                    filter_reason='invalid_saved_analysis')
    previous_status = result.get('target_status')
    if ((reviewed['target_status'] == 'match' and previous_status in ('review', 'excluded'))
            or (options['whole_document_rules'] and previous_status == 'review'
                and reviewed['target_status'] == 'excluded')):
        reviewed.update(accepted=False, review_required=True, target_status='review', filter_reason='review')
        if not reviewed['analysis'].get('grounding_note'):
            reviewed['analysis']['grounding_note'] = '이전 분석의 보류·제외 사유를 원문과 함께 다시 확인해주세요.'
    return dict(result, **reviewed)


def claim(conn, job_id, *, now):
    """One worker owns a job. No automatic time lease can duplicate a live API call."""
    with _atomic(conn):
        job = get_job(conn, job_id)
        if (job['state'] not in ('queued', 'waiting') or job['cancel_requested']
                or job['retry_after'] > now):
            return None
        cached = conn.execute('SELECT result_json FROM news_analysis_cache WHERE cache_key=?',
                              (job['cache_key'],)).fetchone()
        if cached:
            try:
                decoded = json.loads(cached[0])
                valid_cache = isinstance(decoded, dict) and bool(decoded)
            except (TypeError, ValueError):
                valid_cache = False
            if not valid_cache:
                conn.execute('DELETE FROM news_analysis_cache WHERE cache_key=?', (job['cache_key'],))
            else:
                from services import news_custom_analysis
                if job['recipe'] in news_custom_analysis.SUPPORTED_RECIPES:
                    preserve_review = (news_custom_analysis.recipe_options(job['recipe'])['whole_document_rules']
                        and (decoded.get('target_status') == 'review' or decoded.get('review_required') is True))
                    from services.news_analysis_context import NewsAnalysisContext
                    from services.news_target_rules import evaluate_target
                    context = NewsAnalysisContext.from_run(conn, job['run_id'])
                    facts = decoded.get('analysis')
                    status = 'review'
                    checks = {}
                    if isinstance(facts, dict) and type(facts.get('is_relevant')) is bool:
                        decision = evaluate_target(context.profile, facts)
                        status, checks = decision.status, dict(decision.checks)
                        if not facts['is_relevant']:
                            status = 'excluded'
                        elif status == 'match' and (decoded.get('accepted') is not True
                                or decoded.get('review_required') is True
                                or decoded.get('target_status') == 'review'):
                            # Preserve older evidence/coverage holds; target
                            # matching alone is not authority to lift them.
                            status = 'review'
                        if news_custom_analysis.recipe_options(job['recipe'])['applicant_rules']:
                            # Reuse the same source-grounded applicant gate as
                            # the worker. Target matching alone must not turn a
                            # provider/shop recruitment into a review candidate.
                            try:
                                applicant = news_custom_analysis.applicant_status(
                                    facts, json.loads(job['input_json'])['content'])
                            except (TypeError, ValueError, KeyError):
                                applicant = 'review'
                            checks['applicant'] = applicant
                            if applicant == 'excluded':
                                status = 'excluded'
                            elif applicant == 'review' and status == 'match':
                                status = 'review'
                    if preserve_review:
                        # Do not erase a prior v15 hold before the shared
                        # revalidator can see it (not even for is_relevant=false).
                        status = 'review'
                    decoded = dict(decoded, accepted=status == 'match',
                                   review_required=status == 'review', target_status=status,
                                   target_checks=checks, filter_reason=status)
                decoded = _review_saved_result(conn, job, decoded)
                encoded_result = json.dumps(decoded, ensure_ascii=False)
                changed = conn.execute('''UPDATE news_analysis_jobs
                    SET state='complete',result_json=?,claim_token=NULL,reason=NULL
                    WHERE job_id=? AND state IN ('queued','waiting') AND retry_after<=?
                        AND cancel_requested=0''', (encoded_result, job_id, now))
                if changed.rowcount == 1:
                    return None
        token = uuid.uuid4().hex
        changed = conn.execute('''UPDATE news_analysis_jobs
            SET state='running',claim_token=?,attempts=attempts+1,reason=NULL
            WHERE job_id=? AND state IN ('queued','waiting') AND retry_after<=?
                AND cancel_requested=0''',
                               (token, job_id, now))
        if changed.rowcount != 1:
            return None
        return token


def active_claim(conn, job_id, token):
    return conn.execute('''SELECT 1 FROM news_analysis_jobs
        WHERE job_id=? AND state='running' AND claim_token=? AND cancel_requested=0''',
        (job_id, token)).fetchone() is not None


def save_checkpoint(conn, job_id, token, checkpoint):
    """Private worker continuation; not a result, feed item or approval."""
    encoded = _encode(checkpoint)
    if len(encoded.encode('utf-8')) > 2000000:
        raise ValueError('Analysis checkpoint exceeds storage limit')
    with _atomic(conn):
        changed = conn.execute('''UPDATE news_analysis_jobs SET checkpoint_json=?
            WHERE job_id=? AND state='running' AND claim_token=? AND cancel_requested=0''',
            (encoded, job_id, token))
        if changed.rowcount != 1:
            raise ValueError('Analysis checkpoint claim is stale or cancelled')


def complete(conn, job_id, token, result):
    """Only validated successful results belong here; errors must use defer/fail.

    The integration layer must validate the AI schema before calling this method.
    """
    if not isinstance(result, dict) or not result:
        raise ValueError('A nonempty validated result is required')
    encoded = _encode(result)
    with _atomic(conn):
        job = get_job(conn, job_id)
        if job['state'] != 'running' or job['claim_token'] != token:
            raise ValueError('Analysis claim is stale')
        if job['cancel_requested']:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='cancelled',claim_token=NULL WHERE job_id=?",
                         (job_id,))
            return False
        conn.execute('''INSERT INTO news_analysis_cache(cache_key,result_json) VALUES (?,?)
            ON CONFLICT(cache_key) DO NOTHING''', (job['cache_key'], encoded))
        try:
            cache_limit = max(100, min(int(os.getenv('NEWS_ANALYSIS_CACHE_MAX', '5000')), 50000))
        except (TypeError, ValueError):
            cache_limit = 5000
        conn.execute('''DELETE FROM news_analysis_cache WHERE rowid IN (
            SELECT rowid FROM news_analysis_cache ORDER BY rowid DESC LIMIT -1 OFFSET ?
        )''', (cache_limit,))
        conn.execute("UPDATE news_analysis_jobs SET state='complete',result_json=?,claim_token=NULL,checkpoint_json=NULL WHERE job_id=?",
                     (encoded, job_id))
        return True


def defer(conn, job_id, token, *, retry_after, reason, failed=False, unspent_attempt=False):
    if type(retry_after) not in (int, float) or retry_after < 0:
        raise ValueError('retry_after must be a nonnegative timestamp')
    import math
    if (not math.isfinite(retry_after) or type(failed) is not bool
            or type(unspent_attempt) is not bool
            or (unspent_attempt and (failed or reason != 'rate_limited'))):
        raise ValueError('Invalid retry state')
    with _atomic(conn):
        updated = conn.execute('''UPDATE news_analysis_jobs
            SET state=CASE WHEN cancel_requested=1 THEN 'failed' ELSE ? END,
                retry_after=?,reason=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                claim_token=NULL,retry_start_attempt=retry_start_attempt+?
            WHERE job_id=? AND state='running' AND claim_token=?''',
                               ('failed' if failed else 'waiting', retry_after, reason,
                                int(unspent_attempt), job_id, token))
        if updated.rowcount != 1:
            raise ValueError('Analysis claim is stale')


def recover_interrupted_run(conn, run_id):
    """Call ONLY after proving the original run has no live worker.

    Does not launch work or discard original input/profile versions.
    """
    with _atomic(conn):
        return conn.execute('''UPDATE news_analysis_jobs
            SET state=CASE WHEN cancel_requested=1 THEN 'failed' ELSE 'waiting' END,
                claim_token=NULL,retry_after=0,
                reason=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'interrupted' END
            WHERE run_id=? AND state='running' ''', (run_id,)).rowcount


def cancel_job(conn, job_id):
    """Running worker keeps its claim until it drains; do not permit early resume."""
    with _atomic(conn):
        job = get_job(conn, job_id)
        if job['state'] in ('complete', 'failed') or job['cancel_requested']:
            return False
        conn.execute('''UPDATE news_analysis_jobs SET cancel_requested=1,
            state=CASE WHEN state='running' THEN state ELSE 'failed' END,
            reason=CASE WHEN state='running' THEN reason ELSE 'cancelled' END
            WHERE job_id=?''', (job_id,))
        conn.execute('INSERT INTO news_job_events(job_id,event,reason,attempts) VALUES (?,?,?,?)',
                     (job_id, 'cancel_requested', job['reason'], job['attempts']))
        return True


def resume_job(conn, job_id, *, expected_attempts):
    """Explicit user retry, not automatic scheduling. Shared quota guards still apply."""
    if type(expected_attempts) is not int or expected_attempts < 0:
        raise ValueError('Invalid expected attempt count')
    with _atomic(conn):
        job = get_job(conn, job_id)
        if job['state'] != 'failed' or job['attempts'] != expected_attempts:
            raise ValueError('작업이 변경되었거나 아직 실행 중입니다.')
        # Input/recipe failures require a new corrected run, not blind retries.
        allowed = {'cancelled', 'daily_quota_exhausted', 'local_budget_exhausted', 'authentication_required',
                   'retry_limit_reached', 'analysis_error', 'service_unavailable', 'rate_limited'}
        if job['reason'] not in allowed:
            raise ValueError('원래 입력을 수정한 새 작업이 필요합니다.')
        conn.execute('INSERT INTO news_job_events(job_id,event,reason,attempts) VALUES (?,?,?,?)',
                     (job_id, 'manual_resume', job['reason'], job['attempts']))
        conn.execute('''UPDATE news_analysis_jobs SET state='queued',reason=NULL,
            cancel_requested=0,retry_after=0,retry_start_attempt=attempts WHERE job_id=?''', (job_id,))


def list_job_statuses(conn, run_id, *, limit=100, offset=0):
    """UI-safe bounded metadata: omit prompts, raw inputs, results and claim tokens."""
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid pagination')
    cursor = conn.execute('''SELECT job_id,run_id,state,attempts,retry_after,reason,cancel_requested
        FROM news_analysis_jobs WHERE run_id=? ORDER BY rowid LIMIT ? OFFSET ?''', (run_id, limit, offset))
    return [dict(zip((d[0] for d in cursor.description), row)) for row in cursor.fetchall()]

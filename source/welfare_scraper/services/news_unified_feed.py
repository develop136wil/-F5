"""One read-only news feed; materialize selected results only at a final action.

Reading never imports a candidate or calls a provider. User exclusions, archive
state, upload receipts and deleted import receipts take precedence over results.
All mutations recheck the exact displayed fingerprint inside one transaction.
"""
import datetime
import json

from services import news_analysis_store as jobs, news_result_import as imports, legacy_news_analysis, news_legacy_recovery
from services.news_profile_store import get_profile, get_run_snapshot


VIEWS = ('ready', 'review', 'processing', 'records')
ACTIONS = ('upload', 'html', 'dismiss', 'retry')


def _json(value):
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _url(value):
    """Identity normalization only, not a network/SSRF permission check."""
    return news_legacy_recovery.identity_url(value)


def _date(value):
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return None


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _presentation(item, facts, *, manual=False, conn=None):
    from services import notion_service, page_coverage
    from services.news_timing import timing_review_note
    decorated = dict(item, ai_data=facts, manual_news=manual)
    start, end = notion_service.compute_period(decorated)
    attachment = facts.get('source_attachments')
    attachment = attachment if isinstance(attachment, dict) else {}
    notes = [page_coverage.input_note(facts), timing_review_note(decorated)]
    if attachment.get('status') == 'review_required':
        notes.append(str(attachment.get('note') or '첨부파일의 일부 내용은 확인이 필요합니다.'))
    if facts.get('news_import_review') is True:
        notes.append('수집 조건과 대상이 맞는지 확인해주세요.')
    note = ' '.join(dict.fromkeys(value for value in notes if value))[:2000]
    status = notion_service.compute_status(decorated)
    if manual and not start:
        status = '기간 확인 필요'
    from services.news_display_projection import item_presentation
    display = item_presentation(decorated, conn=conn)
    return dict(title=notion_service.clean_title(str(item.get('title') or ''))[:500],
        org=notion_service.site_to_org(str(item.get('site_name') or ''), str(item.get('contact') or '')),
        site_name=str(item.get('site_name') or ''), link=str(item.get('link') or ''),
        start=start, end=end, status=status, target=str(item.get('target') or facts.get('target') or '')[:16000],
        support_detail=display['support_detail'], support_preview=display['support_detail'][:180],
        presentation_kind=display['presentation_kind'], source_note=display['source_note'],
        input_note=note, input_review_required=bool(note), review_required=bool(note),
        attachment_review_required=attachment.get('status') == 'review_required',
        attachment_note=str(attachment.get('note') or '')[:1000], manual_news=manual)


def _stored_rows(conn, profile_id):
    rows = _rows(conn.execute(f'''SELECT id,title,link,site_name,target,support_detail,contact,
        event_date,deadline,event_end_date,ai_data,ai_skip_reason,scraped_at,is_archived,notion_dismissed,notion_page_id,
        {imports.MANUAL_NEWS_SQL} AS manual_news FROM welfare_items'''))
    receipts = _rows(conn.execute('''SELECT i.item_id,i.link,i.outcome,j.job_id,r.profile_id,
        (SELECT json_extract(CASE WHEN json_valid(c.payload_json) THEN c.payload_json ELSE '{}' END,
            '$.published_date') FROM news_run_candidates c WHERE c.job_id=j.job_id
            ORDER BY c.rowid DESC LIMIT 1) AS published_date
        FROM news_result_imports i
        JOIN news_analysis_jobs j ON j.job_id=i.job_id
        JOIN news_run_snapshots r ON r.run_id=j.run_id'''))
    receipt_profiles = {}
    created_profiles = {}
    published_dates = {}
    for receipt in receipts:
        receipt_profiles.setdefault(receipt['item_id'], set()).add(receipt['profile_id'])
        if receipt['outcome'] == 'created':
            created_profiles.setdefault(receipt['item_id'], set()).add(receipt['profile_id'])
        if _date(receipt['published_date']):
            published_dates[receipt['item_id']] = _date(receipt['published_date'])
    by_url, visible = {}, []
    uncertain, upload_recorded = set(), set()
    if _table(conn, 'notion_upload_attempts'):
        for link, page_id in conn.execute('SELECT link,page_id FROM notion_upload_attempts'):
            upload_recorded.add(_url(link))
            if page_id is None:
                uncertain.add(_url(link))
    blocked = {_url(item.get('link')) for item in rows if
               item.get('is_archived') or item.get('notion_dismissed') or item.get('notion_page_id')}
    handled = {_url(row[0]) for row in conn.execute('SELECT link FROM news_result_imports')}
    for item in rows:
        link = _url(item.get('link'))
        if not link:
            continue
        # Only an unadopted, explicitly recorded failure may yield to a later
        # validated result. Successful saved facts and all user choices win.
        item['_legacy_recovery'] = news_legacy_recovery.unadopted_failure(
            item, adopted=link in handled, upload_recorded=link in upload_recorded or link in blocked)
        if link in by_url:
            by_url[link]['_legacy_recovery'] = False
        else:
            by_url[link] = item
        owners = receipt_profiles.get(item['id'], set())
        creators = created_profiles.get(item['id'], set())
        if (profile_id != 'default-news' and profile_id not in owners) or (
                profile_id == 'default-news' and creators and profile_id not in creators):
            continue
        if link in blocked:
            continue
        facts = _json(item.get('ai_data'))
        failed = legacy_news_analysis.failure(facts)
        if failed or (not facts and not item.get('manual_news')):
            reason = (failed or {}).get('reason') or (
                'local_budget_exhausted' if item.get('ai_skip_reason') == 'quota' else 'analysis_incomplete')
            review = reason in _MANUAL_REVIEW_REASONS
            note = (_REASONS if review else _PROCESSING_NOTES).get(reason,
                '수집한 게시글의 분석이 완료되지 않았습니다. 원문과 분석 설정을 확인해주세요.')
            if item['_legacy_recovery']:
                note = news_legacy_recovery.guidance(reason)
            visible.append(dict(id=f"item:{item['id']}", item_id=item['id'], profile_id=profile_id,
                record_id=None, job_id=None, run_id=None, workflow_id=None,
                title=str(item.get('title') or '')[:500], link=item['link'],
                site_name=str(item.get('site_name') or ''), org=str(item.get('site_name') or ''),
                start='', end='', target='', support_detail='', support_preview='',
                status='확인 필요' if review else '분석 미완료',
                view='review' if review else 'processing', selectable=False,
                state='failed' if failed else 'waiting', processing_required=not review,
                review_required=review, input_review_required=review, input_note=note,
                review_reason=reason, published_date=None, publication_status='unknown',
                recovery_action='new_collection' if item['_legacy_recovery'] else None,
                upload_uncertain=link in uncertain,
                fingerprint=jobs._hash({'recipe':'news-feed-legacy-failure-v1', 'item':item})))
            continue
        if not (item.get('manual_news') or facts.get('item_type') == 'event'
                or any(item.get(key) for key in ('deadline', 'event_date', 'event_end_date'))):
            continue
        row = _presentation(item, facts, manual=bool(item.get('manual_news')), conn=conn)
        if row['status'] == '종료':
            continue
        row.update(id=f"item:{item['id']}", item_id=item['id'], profile_id=profile_id,
                   record_id=None, job_id=None, view='ready', selectable=True,
                   published_date=published_dates.get(item['id']),
                   publication_status='known' if item['id'] in published_dates else 'unknown',
                   upload_uncertain=link in uncertain,
                   fingerprint=jobs._hash({'recipe':'news-feed-item-v1', 'item':item,
                                          'upload_uncertain':link in uncertain}))
        visible.append(row)
    # Receipts survive item deletion. Do not resurrect a previously handled URL
    # under a new run/job identifier.
    return by_url, handled, visible


_REASONS = {
    'ai_input_review_required': '개인정보·비밀정보 확인이 필요해 AI 분석을 보류했습니다.',
    'ai_source_review_required': '원문의 생략 표시·본문 형식을 확인해야 해 AI 분석을 보류했습니다. 원문은 보존됩니다. 상세 출처에서 본문이 온전히 수집됐는지 확인해주세요.',
    'ai_input_size_review_required': '원문·추가 요청·분석 형식을 합친 입력이 현재 전송 보호값을 초과해 AI 분석을 보류했습니다. 원문은 보존됩니다. 해당 공고의 상세 출처와 추가 AI 요청 문구를 확인한 뒤 새 수집을 시작해주세요.',
    'ai_response_review_required': '분석 응답을 확인해야 해 보류했습니다.',
    'analysis_incomplete': '분석이 아직 완료되지 않았습니다.',
    'invalid_saved_result': '분석 내용을 확인하지 못했습니다.',
    'relevance_unconfirmed': '수집 조건과의 관련성을 확인하지 못했습니다.',
    'import_text_too_long': '내용이 길어 저장·등록 전 확인이 필요합니다.',
    'outside_publication_period': '설정한 게시 기간에 해당하지 않습니다.',
    'not_article': '게시글이 아닌 안내·메뉴 자료로 분류했습니다.',
    'excluded': '수집 조건에서 제외된 자료입니다.',
    'ended': '신청·행사 기간이 종료된 자료입니다.',
    'semantic_review': '설정한 조건과의 의미상 관련성이 충분히 확인되지 않았습니다. 원문은 그대로 보관합니다.',
}


_MANUAL_REVIEW_REASONS = frozenset({
    'ai_input_review_required', 'ai_input_size_review_required', 'ai_response_review_required',
    'ai_source_review_required',
})
_PROCESSING_NOTES = {
    'local_budget_exhausted': '저장된 원문은 보존됩니다. 원래 작업에서 이어서 진행할 수 있습니다.',
    'daily_quota_exhausted': 'AI 일일 한도로 분석을 멈췄습니다. 한도가 회복된 뒤 원래 작업에서 이어서 진행할 수 있습니다.',
    'authentication_required': 'AI 연결을 확인해야 분석을 진행할 수 있습니다. 수집한 원문은 보관되어 있습니다.',
    'rate_limited': 'AI 요청 간격을 기다리는 중입니다. 수집한 원문은 보관되어 있습니다.',
    'service_unavailable': 'AI 서비스 응답을 받지 못했습니다. 원래 작업의 상태를 확인해주세요.',
    'analysis_error': '분석을 완료하지 못했습니다. 원래 작업의 상태를 확인해주세요.',
    'retry_limit_reached': '분석 재시도 횟수에 도달했습니다. 원래 작업에서 문제를 확인해주세요.',
    'invalid_request': '분석 요청을 처리하지 못했습니다. 분석 설정과 원래 작업의 상태를 확인해주세요.',
    'cancelled': '사용자가 중지한 분석 기록입니다. 수집한 원문은 보관되어 있습니다.',
    'interrupted': '분석 작업이 중단되었습니다. 원래 작업의 상태를 확인해주세요.',
    'unsupported_analysis': '이전 분석 방식의 기록입니다. 현재 새 소식 분석을 완료한 자료로 표시하지 않으며 원문과 기록은 보관합니다.',
}
_PROCESSING_STATES = {
    'queued': ('분석 대기', '수집한 원문이 분석을 기다리고 있습니다. 원래 작업에서 진행 상태를 확인할 수 있습니다.'),
    'running': ('분석 중', '수집한 원문을 분석하고 있습니다. 분석 결과에 따라 찾은 소식에 표시됩니다.'),
    'waiting': ('분석 대기', '분석이 잠시 멈춰 있습니다. 원래 작업에서 진행 상태와 재개 가능 여부를 확인해주세요.'),
    'failed': ('분석 미완료', '분석을 완료하지 못했습니다. 원래 작업과 원문은 계속 확인할 수 있습니다.'),
    'analysis_unconfigured': ('분석 설정 대기', '원문을 수집했지만 분석 설정이 준비되지 않았습니다. 원래 작업에서 설정과 진행 상태를 확인해주세요.'),
    'semantic_candidate': ('관련성 분석 대기', '수집한 원문의 관련성 분석이 아직 완료되지 않았습니다.'),
    'complete': ('이전 분석 기록', '현재 방식으로 분석을 완료한 새 소식이 아닙니다. 원문과 이전 기록은 보관합니다.'),
}


def _workflow_refs(conn, profile_id):
    """Navigation metadata only; the existing status endpoint owns resume checks.

    Do not call get_workflow here: it can create/migrate schema during a GET.
    Older jobs can legitimately have no workflow, so no resume promise is made.
    """
    if not _table(conn, 'news_profile_workflows'):
        return {}
    return {row['run_id']: row for row in _rows(conn.execute('''
        SELECT w.run_id,w.workflow_id,w.state AS workflow_state
        FROM news_profile_workflows w JOIN news_run_snapshots r ON r.run_id=w.run_id
        WHERE r.profile_id=?''', (profile_id,)))}


def _latest_records(conn, profile_id):
    """Newest record owns a URL, including excluded records: no old resurfacing."""
    # The read-only overview must not pull every failed/pending source body into
    # Python on every poll. Only complete results are loaded for local grounding.
    rows = _rows(conn.execute('''WITH records AS (SELECT j.job_id,j.run_id,j.state,j.reason,
        j.input_json AS payload_json, r.rowid AS run_order,
        COALESCE((SELECT MAX(c.rowid) FROM news_run_candidates c WHERE c.job_id=j.job_id),0) AS item_order,
        j.rowid AS record_order,
        NULL AS candidate_key FROM news_analysis_jobs j
        JOIN news_run_snapshots r ON r.run_id=j.run_id WHERE r.profile_id=?
        UNION ALL
        SELECT NULL,c.run_id,c.route,NULL,c.payload_json,r.rowid,c.rowid,c.rowid,c.candidate_key
        FROM news_run_candidates c JOIN news_run_snapshots r ON r.run_id=c.run_id
        WHERE r.profile_id=? AND c.job_id IS NULL)
        SELECT job_id,run_id,state,reason,run_order,item_order,record_order,candidate_key,
            json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.title') AS title,
            json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.link') AS link,
            json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.site_name') AS site_name,
            json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.published_date') AS published_date,
            json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.selection_reason') AS selection_reason
        FROM records ORDER BY run_order DESC,item_order DESC,record_order DESC''', (profile_id, profile_id)))
    seen = set()
    for record in rows:
        payload = {name:record.pop(name) for name in ('title','link','site_name','published_date','selection_reason')}
        key = _url(payload.get('link'))
        if not key or key in seen:
            continue
        seen.add(key)
        yield record, payload, key


def _read_all(conn, profile_id):
    if profile_id != 'default-news' or conn.execute(
            'SELECT 1 FROM news_profiles WHERE profile_id=?', (profile_id,)).fetchone():
        get_profile(conn, profile_id)
    existing, handled, feed = _stored_rows(conn, profile_id)
    snapshots = {}
    workflows = _workflow_refs(conn, profile_id)
    for record, payload, key in _latest_records(conn, profile_id):
        previous = existing.get(key)
        if key in handled or (previous and not previous['_legacy_recovery']):
            continue
        job_id = record['job_id']
        identity = ('job:' + job_id if job_id else
                    'candidate:' + record['run_id'] + ':' + record['candidate_key'])
        metadata = payload
        if job_id:
            candidate = conn.execute('''SELECT
                json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.published_date'),
                json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,'$.selection_reason'),route
                FROM news_run_candidates WHERE job_id=? ORDER BY rowid DESC LIMIT 1''',
                                     (job_id,)).fetchone()
            if candidate:
                metadata = dict(payload, published_date=candidate[0], selection_reason=candidate[1], candidate_route=candidate[2])
        published = _date(metadata.get('published_date'))
        workflow = workflows.get(record['run_id'], {})
        row = dict(id=identity, item_id=None, profile_id=profile_id, record_id=identity, job_id=job_id,
                   run_id=record['run_id'], workflow_id=workflow.get('workflow_id'),
                   workflow_state=workflow.get('workflow_state'), processing_required=False,
                   title=str(payload.get('title') or '')[:500], site_name=str(payload.get('site_name') or ''),
                   org=str(payload.get('site_name') or ''), link=str(payload.get('link') or ''),
                   start='', end='', status='확인 필요', target='', support_detail='', support_preview='',
                   view='review', selectable=False, upload_uncertain=False, review_required=True,
                   input_review_required=True, input_note='', published_date=published,
                   publication_status='known' if published else 'unknown', state=record['state'])
        reason = metadata.get('selection_reason') or record['reason'] or 'analysis_incomplete'
        fingerprint_input = {'record':record, 'payload':payload, 'metadata':metadata}
        # An intake exclusion is stronger than any cached/completed analysis.
        # Never let a linked job re-admit a known menu or out-of-period page.
        if (record['state'] == 'excluded' or metadata.get('candidate_route') == 'excluded'
                or metadata.get('selection_reason') in ('not_article', 'outside_publication_period')):
            row.update(view='records', status='제외')
            reason = metadata.get('selection_reason') or 'excluded'
        elif job_id and record['state'] == 'complete':
            try:
                job = jobs.get_job(conn, job_id)
                result = _json(job.get('result_json'))
                fingerprint_input['result'] = result
                fingerprint_input['source_fingerprint'] = jobs._hash({
                    'input_json':job['input_json'], 'recipe':job['recipe'], 'model':job['model']})
                if record['run_id'] not in snapshots:
                    snapshots[record['run_id']] = get_run_snapshot(conn, record['run_id'])
                source, facts, _import_fingerprint, _revision = imports._read(
                    conn, profile_id, job_id, reviewed_job=job, run_snapshot=snapshots[record['run_id']])
                item = dict(source, **{name:facts.get(name) for name in (
                    'target','support_detail','contact','deadline')})
                row.update(_presentation(item, facts, manual=True))
                if imports._ended(source, facts):
                    row.update(view='records', selectable=False, status='종료')
                    reason = 'ended'
                else:
                    row.update(view='ready', selectable=True)
                    reason = ''
            except imports.ImportUnavailable as error:
                reason = error.reason
                if reason == 'excluded':
                    row.update(view='records', status='제외')
                elif reason == 'unsupported_analysis':
                    row['view'] = 'processing'
            except (ValueError, TypeError, KeyError):
                reason = 'invalid_saved_result'
        elif reason in _MANUAL_REVIEW_REASONS:
            pass  # Input/response safety holds still need explicit human review.
        elif record['state'] == 'semantic_review':
            reason = 'semantic_review'
        else:
            row['view'] = 'processing'
        if row['view'] == 'processing':
            status, note = _PROCESSING_STATES.get(record['state'],
                ('분석 미완료', '분석이 아직 완료되지 않았습니다. 원래 작업과 원문을 확인해주세요.'))
            row.update(status=status, processing_required=True, review_required=False,
                       input_review_required=False, input_note=_PROCESSING_NOTES.get(reason, note))
        elif reason:
            row['input_note'] = _REASONS.get(reason, '자료의 분석 상태를 확인해주세요.')
        row['review_reason'] = reason
        if previous:
            # Failed/unfinished/newly excluded analyses must not masquerade as
            # recovery. Keep the original failure until a usable result exists.
            if row['view'] != 'ready' or not row['selectable']:
                continue
            feed = [old for old in feed if old.get('item_id') != previous['id']]
            row['recovery_item_id'] = previous['id']
            fingerprint_input['legacy_failure'] = previous
        row['fingerprint'] = jobs._hash({'recipe':'news-feed-result-v1', **fingerprint_input})
        feed.append(row)
    return feed


def read_feed(conn, profile_id='default-news', *, view='ready', limit=10, offset=0):
    if view not in VIEWS or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid feed pagination')
    items = _read_all(conn, profile_id)
    counts = {name:sum(row['view'] == name for row in items) for name in VIEWS}
    selected = [row for row in items if row['view'] == view]
    return dict(items=selected[offset:offset+limit], count=len(selected), total=len(selected), counts=counts,
                limit=limit, offset=offset, has_more=len(selected) > offset+limit, ai_called=False)


def prepare(conn, profile_id, selections, *, action, confirmed):
    """Final user intent, not a second import button or an automatic approval.

Only caller-supplied, explicitly confirmed IDs/fingerprints authorize local
materialization. The existing upload/export/dismiss endpoint remains the final
action. One bad/stale selection rolls back the entire local preparation.
"""
    if confirmed is not True or action not in ACTIONS or not 1 <= len(selections) <= 100:
        raise ValueError('Explicit final action confirmation required')
    if len({selection['id'] for selection in selections}) != len(selections):
        raise ValueError('Duplicate selected item')
    with imports._write_transaction(conn):
        current = {row['id']:row for row in _read_all(conn, profile_id)}
        chosen = []
        for selection in selections:
            row = current.get(selection['id'])
            if (row is None or not row['selectable'] or row['view'] != 'ready'
                    or row['fingerprint'] != selection['fingerprint']):
                raise ValueError('News selection changed')
            if action == 'upload' and row['upload_uncertain']:
                raise ValueError('Upload outcome must be checked first')
            if action == 'retry' and (not row['upload_uncertain'] or row['item_id'] is None):
                raise ValueError('No uncertain upload to retry')
            chosen.append(row)
        ids = []
        for row in chosen:
            item_id = row['item_id']
            if item_id is None:
                preview = imports.preview(conn, profile_id, row['job_id'])
                if preview['status'] != 'ready':
                    raise ValueError('News destination changed')
                imported = imports.import_result(conn, profile_id, row['job_id'],
                    fingerprint=preview['fingerprint'], confirmed=confirmed)
                item_id = imported['item_id']
                if item_id is None:
                    raise ValueError('News destination missing')
            ids.append(item_id)
        return dict(ids=ids, action=action, ai_called=False, notion_uploaded=False)

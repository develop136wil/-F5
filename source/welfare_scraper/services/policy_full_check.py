"""One user action, conditional stages, and no automatic Notion writes."""
from services.policy_full_check_budget import ai_pause_reason, public_ai_budget
from services.secret_redaction import redact_secrets


def get_plan(db_keys):
    """Local estimate only; Notion sync can discover additional policies."""
    import database
    from services import policy_sync as ps
    keys = ps._active_configured_keys(db_keys)
    rows = database.execute_query(
        'SELECT audited_at FROM notion_policies WHERE db_key IN (%s)' %
        ','.join('?' for _ in keys), tuple(keys), raise_on_error=True) if keys else []
    style = ps.preview_style_work(db_keys)
    budget = public_ai_budget()
    return {
        'policy_count': len(rows),
        'initial_count': sum(row['audited_at'] is None for row in rows),
        'pending_count': len(ps._automatic_recompute_ids(db_keys)),
        'style_count': style['eligible_count'],
        'style_configured': style['configured'],
        'ai_remaining': budget['remaining'],
        'pause_reason': budget['reason'],
        'estimate_note': ('저장된 정책 기준의 예상 대상 수입니다. Notion에서 새로 불러오는 정책과 '
                          '출처 변경에 따라 늘어날 수 있으며, 대상 수와 AI 요청 수는 다릅니다.'),
    }


def _stage(ps, phase, message):
    ps.ONE_CLICK_STATUS.update(phase=phase, message=message, checked=0, total=0)


def _completion_message(status, state, pause_reason):
    prefix = {'paused': '점검 잠시 멈춤', 'partial': '점검 일부 완료', 'completed': '점검 완료'}[state]
    message = (f"{prefix} · 최초 비교 {status['initial_compared']}건 · "
               f"변경 제안 {status['changed']}건 · 문장 정리 {status['styled']}건")
    failures = []
    if status['ai_failed']:
        failures.append(f"AI 분석 미완료 {status['ai_failed']}건")
    if status['style_failed']:
        failures.append(f"문장 정리 실패 {status['style_failed']}건")
    if status['style_rejected_fields']:
        failures.append(f"문장 정리 검증 보류 {status['style_rejected_fields']}개 항목")
    if status['embedding_failed']:
        failures.append(f"제목 검색 데이터 준비 실패 {status['embedding_failed']}건")
    if status.get('verification_pending'):
        failures.append(f"이전 반영 결과 확인 필요 {status['verification_pending']}건")
    if failures:
        message += ' · ' + ' · '.join(failures) + '.'
    # The details panel exists only for actual notes, not every failed AI or
    # auxiliary embedding request. Never send the user to an absent review row.
    has_reviews = bool(status['review_notes'])
    if has_reviews:
        message += ' 아래 ‘확인이 필요한 정책’에 표시된 사유를 먼저 확인해주세요.'
    if status['style_rejected_fields']:
        message += ' 문장 정리에서 보류한 항목은 변경 제안의 원문과 표현을 확인해주세요. 정리 전 제안은 유지됩니다.'
    if pause_reason:
        message += ' AI 연결·사용 한도를 확인한 뒤 ‘정책 정보 최신인지 확인하기’를 눌러 이어가세요.'
    elif status['ai_failed'] or status['style_failed'] or status['embedding_failed']:
        message += (' 필요한 확인을 마친 뒤' if has_reviews else ' 연결 상태를 확인하고 잠시 후')
        message += ' ‘정책 정보 최신인지 확인하기’를 다시 눌러 완료하지 못한 작업을 재시도하세요.'
    if state != 'completed':
        message += ' 완료한 비교와 제안은 보존됩니다. 기존 Notion 내용은 유지됩니다.'
    message += ' Notion에는 자동 반영하지 않았습니다.'
    return message


async def run(db_keys):
    import database
    from services import policy_sync as ps
    status = ps.ONE_CLICK_STATUS
    status.update(running=True, phase='sync', state='running', checked=0, total=0,
                  message='Notion에서 최신 내용 불러오는 중…',
                  ai_required=0, ai_completed=0, ai_rate_waited=0, ai_failed=0,
                  ai_preflight_held=0, changed=0, initial_compared=0, styled=0,
                  style_failed=0, style_rejected_fields=0,
                  embedding_completed=0, embedding_failed=0,
                  review_count=0, review_notes=[], verification_pending=0, pause_reason='', stages=[])
    try:
        synced = await ps.sync_policies(db_keys, defer_embeddings=True)
        if not synced.get('ok'):
            message = 'Notion 내용을 불러오지 못해 점검을 중단했습니다. 기존 자료는 유지됩니다.'
            error = synced.get('error')
            if isinstance(error, str):
                detail = ' '.join(redact_secrets(error).split())[:600]
                if detail:
                    message += ' ' + detail
            status.update(state='error', message=message)
            return {**synced, 'state': 'error'}
        status['stages'].append('sync')
        status['verification_pending'] = synced.get('verification_pending', 0)

        # Recompute only current, fully validated evidence with obsolete diff
        # logic. Stale extraction/snapshots are handled by fresh checking below.
        _stage(ps, 'recompute', '저장된 제안 중 재점검이 필요한 내용 확인 중…')
        await ps.recompute_diffs(db_keys, only_needed=True)
        status['stages'].append('recompute')

        # First comparisons share the same source pass/extraction as changes:
        # do not run the standalone initial audit again after this step.
        _stage(ps, 'check', '출처 변경과 아직 비교하지 않은 정책 확인 중…')
        checked = await ps.check_policies(db_keys, include_initial=True, stop_on_quota=True)
        status['stages'].append('check')
        if not checked.get('ok', False):
            status.update(state='error', message='출처 점검을 완료하지 못했습니다. 저장된 결과는 유지됩니다. 다시 점검해주세요.')
            return {**checked, 'state': 'error'}
        for name in ('ai_required', 'ai_completed', 'ai_rate_waited', 'ai_failed',
                     'ai_preflight_held', 'changed', 'review_count', 'review_notes', 'initial_compared'):
            status[name] = checked.get(name, [] if name == 'review_notes' else 0)
        pause_reason = checked.get('pause_reason', '')
        if not pause_reason:
            style = await database.run_db(ps.preview_style_work, db_keys)
            if style['configured'] and style['eligible_count']:
                _stage(ps, 'style', '새 변경 제안을 설정한 작성 스타일로 정리 중…')
                styled = await ps.restyle_pending_diffs(db_keys, only_needed=True)
                status['styled'] = styled.get('styled', 0)
                status['style_failed'] = styled.get('failed', 0)
                status['style_rejected_fields'] = styled.get('rejected_fields', 0)
                pause_reason = styled.get('pause_reason', '')
                if not styled.get('ok', True) and not pause_reason and not status['style_failed']:
                    status['style_failed'] = 1
                status['stages'].append('style')
        if not pause_reason:
            _stage(ps, 'embedding', '정책 제목 검색 데이터 확인 중…')
            embedded = await fill_missing_embeddings(db_keys)
            pause_reason = embedded.get('pause_reason', '')
            status['embedding_completed'] = embedded.get('completed', 0)
            status['embedding_failed'] = embedded.get('failed', 0)
            status['stages'].append('embedding')
        state = ('paused' if pause_reason else
                 'partial' if status['review_count'] or status['review_notes'] or status['ai_failed']
                 or status['style_failed'] or status['style_rejected_fields'] or status['embedding_failed']
                 or status['verification_pending']
                 else 'completed')
        message = _completion_message(status, state, pause_reason)
        status.update(state=state, pause_reason=pause_reason, message=message)
        return {**checked, 'ok': True, 'state': state, 'pause_reason': pause_reason,
                'synced': synced.get('synced', 0), 'styled': status['styled'],
                'verification_pending': status['verification_pending'],
                **{key: status[key] for key in ('style_failed', 'style_rejected_fields',
                                               'embedding_completed', 'embedding_failed')}}
    except BaseException:
        status.update(state='error', message='점검을 완료하지 못했습니다. 저장한 비교 결과는 유지됩니다. 다시 점검해주세요.')
        raise
    finally:
        for child in (ps.SYNC_STATUS, ps.POLICY_STATUS, ps.AUDIT_STATUS, ps.RECOMPUTE_STATUS, ps.STYLE_STATUS):
            child['running'] = False
        status.update(running=False, phase='done')


def _title_embedding_pause_reason(es):
    reason = ai_pause_reason(model=es.EMBED_MODEL)
    if reason:
        return reason
    # Legacy embedding daily holds are also persisted by this service. Some
    # provider embedding quota metrics do not populate the shared ledger hold.
    unavailable = es.get_unavailable_reason()
    return {'daily_quota_exhausted': 'daily_limit',
            'missing_api_key': 'ai_connection_required'}.get(unavailable, '')


async def fill_missing_embeddings(db_keys):
    """Auxiliary title search data uses only budget left after policy work."""
    import database
    from services import policy_sync as ps, ai_coordinator, embedding_service as es
    keys = ps._active_configured_keys(db_keys)
    rows = await database.run_db(database.execute_query,
        'SELECT id,title FROM notion_policies WHERE title_embedding IS NULL '
        'AND title IS NOT NULL AND title NOT IN (?,?) AND db_key IN (%s)' % ','.join('?' for _ in keys),
        ('', '(제목없음)', *keys), raise_on_error=True) if keys else []
    offset, completed, failed = 0, 0, 0
    while offset < len(rows):
        reason = _title_embedding_pause_reason(es)
        if reason:
            return {'completed': completed, 'failed': failed, 'pause_reason': reason}
        allowance = ai_coordinator.input_request_budget(es.EMBED_MODEL)
        if allowance < 1:
            return {'completed': completed, 'failed': failed, 'pause_reason': 'quota_state_unavailable'}
        batch = rows[offset:offset + min(ps._EMBED_BATCH_CHUNK, allowance)]
        vectors = await es.embed_texts_batch_async([row['title'] for row in batch])
        for i, row in enumerate(batch):
            vector = vectors[i] if i < len(vectors) else None
            if vector is not None:
                await ps._db_write_strict(
                    'UPDATE notion_policies SET title_embedding=? WHERE id=? AND title=? AND title_embedding IS NULL',
                    (es.to_blob(vector), row['id'], row['title']))
                completed += 1
            else:
                failed += 1
        offset += len(batch)
        ps.ONE_CLICK_STATUS.update(checked=offset, total=len(rows),
            embedding_completed=completed, embedding_failed=failed,
            message=f'정책 제목 검색 데이터 정리 중… ({offset}/{len(rows)})')
        if any(vector is None for vector in vectors) or len(vectors) < len(batch):
            reason = _title_embedding_pause_reason(es)
            if reason:
                return {'completed': completed, 'failed': failed, 'pause_reason': reason}
    return {'completed': completed, 'failed': failed, 'pause_reason': ''}

"""Dismiss terminal history cards without deleting their news or provenance.

The original workflow timestamp remains intact for record retention. No extra
history table or tombstone is created. Callers must hold the workflow OS lease.
"""
from services.news_profile_store import _atomic, get_profile
from services.news_profile_workflow import ensure_schema, get_workflow


def clear_history(conn, profile_id, *, workflow_ids=None, protected_ids=()):
    if workflow_ids is not None and (not isinstance(workflow_ids, list)
            or not 1 <= len(workflow_ids) <= 100
            or any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in workflow_ids)):
        raise ValueError('Invalid workflow selection')
    get_profile(conn, profile_id)
    with _atomic(conn):
        ensure_schema(conn)
        rows = conn.execute('''SELECT w.workflow_id FROM news_profile_workflows w
            JOIN news_run_snapshots r ON r.run_id=w.run_id
            WHERE r.profile_id=? AND w.history_dismissed=0
            AND w.state IN ('complete','cancelled','failed','waiting')''', (profile_id,)).fetchall()
        requested = set(workflow_ids) if workflow_ids is not None else None
        protected = set(protected_ids)
        selected = [identity for (identity,) in rows
                    if identity not in protected and (requested is None or identity in requested)
                    and get_workflow(conn, identity)['can_clear_history']]
        conn.executemany('UPDATE news_profile_workflows SET history_dismissed=1 WHERE workflow_id=?',
                         [(identity,) for identity in selected])
        return {'cleared': len(selected), 'news_deleted': False}

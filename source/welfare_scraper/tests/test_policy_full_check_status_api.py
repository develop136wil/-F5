"""Exercise only extracted route definitions: no app/config/credentials are loaded."""
import ast
import asyncio
import copy
import sys
import types
from pathlib import Path

import pytest


ROUTES = ['policy_sync_endpoint', 'policy_check_endpoint', 'policy_audit_endpoint',
          'policy_recompute_endpoint', 'policy_restyle_endpoint']


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('lock_ok', [True, False])
def test_new_standalone_task_replaces_terminal_parent_only_after_lock(monkeypatch, route, lock_ok):
    source = ast.parse((Path(__file__).parents[1] / 'api_server.py').read_text(encoding='utf-8'))
    names = set(ROUTES) | {'_clear_completed_full_check_status'}
    definitions = [copy.deepcopy(node) for node in source.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in definitions:
        node.decorator_list = []
    status = {'running': False, 'state': 'paused', 'review_count': 2, 'message': 'old full check'}
    before = dict(status)
    lock_events = []

    class BusyError(Exception):
        def __init__(self, **kwargs):
            super().__init__(kwargs)

    async def acquire(*_args):
        assert status == before
        lock_events.append('acquired' if lock_ok else 'rejected')
        if not lock_ok:
            raise BusyError(status_code=409)

    async def try_lock(*_args):
        assert status == before
        lock_events.append('acquired' if lock_ok else 'rejected')
        return lock_ok

    policy_sync = types.SimpleNamespace(ONE_CLICK_STATUS=status,
        try_acquire_maintenance_lock=try_lock, maintenance_busy_message=lambda: 'busy')
    monkeypatch.setitem(sys.modules, 'services', types.SimpleNamespace(policy_sync=policy_sync))
    namespace = {'PolicyDbKeys': object, 'BackgroundTasks': object, 'HTTPException': BusyError,
                 '_acquire_policy_ai_job': acquire}
    for name in ['sync', 'check', 'audit', 'recompute', 'restyle']:
        namespace[f'_run_policy_{name}'] = lambda *_args: None
    exec(compile(ast.Module(body=definitions, type_ignores=[]), '<isolated-policy-routes>', 'exec'), namespace)
    queued = []
    background = types.SimpleNamespace(add_task=lambda *args: queued.append(args))
    request = types.SimpleNamespace(db_keys=['fixture'])
    if lock_ok:
        assert asyncio.run(namespace[route](request, background)) == {'status': 'started'}
        assert status == {'running': False, 'state': 'idle', 'phase': '', 'checked': 0, 'total': 0, 'message': ''}
        assert len(queued) == 1
    else:
        with pytest.raises(BusyError):
            asyncio.run(namespace[route](request, background))
        assert status == before
        assert not queued
    assert len(lock_events) == 1
    status.update(running=True, state='running')
    before_running = dict(status)
    namespace['_clear_completed_full_check_status']()
    assert status == before_running

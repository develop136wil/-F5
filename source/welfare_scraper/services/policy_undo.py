"""Durable, single-property receipts. Never infer a legacy undo value."""
import copy
import json
import uuid
import database
from services import notion_service


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS policy_apply_receipts (
        id TEXT PRIMARY KEY, policy_id INTEGER NOT NULL, page_id TEXT NOT NULL,
        field TEXT NOT NULL, fkey TEXT NOT NULL, property_id TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL, before_value TEXT NOT NULL,
        state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    if 'operation' not in {row[1] for row in conn.execute('PRAGMA table_info(policy_apply_receipts)')}:
        # Old uncertain receipts cannot tell apply from undo; never infer it.
        conn.execute("ALTER TABLE policy_apply_receipts ADD COLUMN operation TEXT NOT NULL DEFAULT 'unknown'")


def writable(prop):
    """Keep writable content/formatting; reject unsupported or truncated values."""
    kind = prop.get('type') or next((k for k in ('title','rich_text','multi_select','select','number','url','email','phone_number','date','checkbox') if k in prop), None)
    if kind not in ('title','rich_text','multi_select','select','number','url','email','phone_number','date','checkbox'):
        raise ValueError('이 속성 형식은 안전한 복원을 지원하지 않습니다.')
    value = copy.deepcopy(prop[kind])
    if kind in ('title','rich_text'):
        if not isinstance(value, list) or len(value) >= 25:
            raise ValueError('전체 원문을 확인할 수 없어 적용을 중단했습니다.')
        cleaned = []
        for item in value:
            if item.get('type','text') != 'text':
                raise ValueError('멘션 등 특수 서식이 있는 속성은 Notion에서 직접 수정해주세요.')
            cleaned.append({'type':'text','text':item['text'], 'annotations':item.get('annotations', {
                'bold':False,'italic':False,'strikethrough':False,'underline':False,'code':False,'color':'default'})})
        value = cleaned
    elif kind == 'multi_select':
        value = [{'id':v['id']} if v.get('id') else {'name':v['name']} for v in value]
    elif kind == 'select' and value:
        value = {'id':value['id']} if value.get('id') else {'name':value['name']}
    return {kind:value}


def matches(prop, payload):
    # Notion adds option IDs and default annotations to PATCH values.
    kind = next(iter(payload))
    if kind == 'multi_select':
        expected = payload[kind]
        actual = prop.get(kind, [])
        return len(actual) == len(expected) and all(any(
            a.get('id') == e['id'] if 'id' in e else a.get('name') == e.get('name') for a in actual) for e in expected)
    if kind == 'select' and payload[kind]:
        expected=payload[kind]; actual=prop.get(kind) or {}
        return actual.get('id') == expected['id'] if 'id' in expected else actual.get('name') == expected.get('name')
    try:
        left, right = writable(prop), writable(payload)
        if kind in ('title','rich_text'):
            # A missing link is equivalent to Notion's explicit null link.
            for obj in (left,right):
                for item in obj[kind]: item['text'].setdefault('link',None)
        return left == right
    except (KeyError, TypeError, ValueError):
        return False


def prepare(policy_id, field, target, page, old_value):
    name, after = next(iter(target['properties'].items()))
    prop = page.get('properties',{}).get(name)
    if not prop or not prop.get('id'):
        raise ValueError('적용 전 속성 원본을 확보하지 못했습니다.')
    before=writable(prop)
    receipt=uuid.uuid4().hex
    with database.get_connection() as conn:
        schema(conn)
        conn.execute('INSERT INTO policy_apply_receipts (id,policy_id,page_id,field,fkey,property_id,before_json,after_json,before_value,state,operation) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (receipt,policy_id,target['page_id'],field,target['fkey'],prop['id'],json.dumps(before),json.dumps(after),json.dumps(old_value),'prepared','apply'))
        conn.commit()
    return receipt


def mark(receipt, state, operation=None):
    with database.get_connection() as conn:
        schema(conn)
        if operation is None:
            conn.execute('UPDATE policy_apply_receipts SET state=? WHERE id=?',(state,receipt))
        else:
            if operation not in ('apply', 'undo'):
                raise ValueError('Unknown receipt operation')
            conn.execute('UPDATE policy_apply_receipts SET state=?,operation=? WHERE id=?',
                         (state,operation,receipt))
        conn.commit()


def _display_payload(payload):
    """Display only the writable value, never expose a raw Notion object."""
    if not isinstance(payload, dict) or len(payload) != 1:
        return ''
    kind, value = next(iter(payload.items()))
    if kind in ('rich_text', 'title'):
        return ''.join(str(item.get('text', {}).get('content', ''))
                       for item in value if isinstance(item, dict))
    if kind == 'multi_select':
        return [str(item.get('name') or item.get('id') or '')
                for item in value if isinstance(item, dict)]
    if kind == 'select':
        return str((value or {}).get('name') or (value or {}).get('id') or '')
    if kind == 'date':
        return ' ~ '.join(str((value or {}).get(key)) for key in ('start', 'end')
                          if (value or {}).get(key))
    return value if isinstance(value, (str, int, float, bool)) else ''


def _receipt_table_exists(conn):
    # sqlite_master stores the declared spelling, while SQLite resolves the
    # ASCII table identifier without case. Use the same rule for all readers.
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
        ('policy_apply_receipts',)).fetchone() is not None


def mark_restored_history():
    """Restored receipts are history, not authority to modify a remote page."""
    with database.get_connection() as conn:
        if _receipt_table_exists(conn):
            conn.execute("UPDATE policy_apply_receipts SET state='restored'")
            if conn.execute("SELECT 1 FROM policy_apply_receipts WHERE state IS NOT 'restored' LIMIT 1").fetchone():
                raise ValueError('복원 이력을 무효화하지 못해 복원을 중단합니다.')
            conn.commit()



def applied_records(policy_id=None, page_id=None):
    """Read receipt history without creating tables or mutating saved proposals.

    The stable page ID survives a policy collection move/local ID replacement.
    A prepared receipt is explicitly uncertain, not evidence of a completed
    write; an explicit undo request still checks the exact live after value.
    """
    with database.get_connection() as conn:
        if not _receipt_table_exists(conn):
            return []
        if page_id is None and policy_id is None:
            records = conn.execute("SELECT * FROM policy_apply_receipts WHERE state IN ('applied','prepared','undoing','uncertain') ORDER BY created_at DESC,rowid DESC").fetchall()
        else:
            column, value = ('page_id', page_id) if page_id is not None else ('policy_id', policy_id)
            records = conn.execute('SELECT * FROM policy_apply_receipts WHERE ' + column +
                                   "=? AND state IN ('applied','prepared','undoing','uncertain') ORDER BY created_at DESC,rowid DESC",
                                   (value,)).fetchall()
    result = []
    for row in records:
        try:
            item = {key: row[key] for key in ('id', 'policy_id', 'page_id', 'field',
                                             'fkey', 'property_id', 'state', 'created_at')}
            item['before_value'] = json.loads(row['before_value'])
            item['after_value'] = _display_payload(json.loads(row['after_json']))
            item['needs_verification'] = row['state'] != 'applied'
            item['can_undo'] = item['undo_available'] = row['state'] in ('applied', 'prepared')
            item['guidance'] = ('' if item['can_undo'] else
                                'Notion에서 현재 반영 결과를 확인해주세요. 불확실한 요청은 다시 보내지 않습니다.')
            result.append(item)
        except (ValueError, TypeError, KeyError):
            # Corrupt records never create an inferred restore value.
            continue
    return result


async def undo(policy_id, field, receipt):
    # Caller holds both the maintenance gate and policy lock.
    with database.get_connection() as conn:
        schema(conn)
        record=conn.execute('SELECT rowid AS receipt_order,* FROM policy_apply_receipts WHERE id=? AND field=?',(receipt,field)).fetchone()
        policy=conn.execute('SELECT page_id,pending_diff,notion_fields FROM notion_policies WHERE id=?',(policy_id,)).fetchone()
        newer = conn.execute("SELECT 1 FROM policy_apply_receipts WHERE page_id=? AND property_id=? AND rowid>? AND state IN ('applied','prepared','undoing','uncertain') LIMIT 1",
                             (record['page_id'], record['property_id'], record['receipt_order'])).fetchone() if record else None
    if not record or not policy or record['state'] not in ('applied', 'prepared') or policy['page_id'] != record['page_id']:
        return {'ok':False,'error':'UNDO_UNAVAILABLE','message':'취소할 적용 기록이 없거나 이미 처리되었습니다.'}
    if newer:
        return {'ok':False,'error':'UNDO_STALE','message':'같은 속성에 더 최근 반영 기록이 있어 이 기록은 취소하지 않았습니다. 최신 기록을 먼저 확인해주세요.'}
    pd=json.loads(policy['pending_diff'] or '{}')
    target=next((f for f in pd.get('fields',[]) if f.get('field') == field and f.get('applied') and f.get('undo_id') == receipt),None)
    page=await notion_service.get_page(record['page_id'])
    prop=next((p for p in (page or {}).get('properties',{}).values() if p.get('id') == record['property_id']),None)
    if not prop or not matches(prop,json.loads(record['after_json'])):
        return {'ok':False,'error':'UNDO_STALE','message':'Notion의 현재 내용이 적용한 값과 다르거나 확인할 수 없어 취소하지 않았습니다. 직접 수정한 내용을 확인해주세요.'}
    before=json.loads(record['before_json'])
    await database.run_db(mark,receipt,'undoing','undo')
    result=await notion_service.update_page_properties(record['page_id'],{record['property_id']:before})
    if not result.get('ok'):
        await database.run_db(mark,receipt,'uncertain' if result.get('error') == 'UPDATE_UNCERTAIN' else 'applied')
        return result
    verified=await notion_service.get_page(record['page_id'])
    actual=next((p for p in (verified or {}).get('properties',{}).values() if p.get('id') == record['property_id']),None)
    if not actual or not matches(actual,before):
        await database.run_db(mark,receipt,'uncertain')
        return {'ok':False,'error':'UPDATE_UNCERTAIN','message':'취소 요청 후 결과를 확인하지 못했습니다. 재시도하지 말고 Notion 내용을 확인해주세요.'}
    with database.get_connection() as conn:
        fields=json.loads(policy['notion_fields'] or '{}')
        fields[record['fkey']]=json.loads(record['before_value'])
        if target:
            target['applied']=False
            target.pop('acceptance',None); target.pop('undo_id',None)
            # An undone old proposal must be rechecked against restored live
            # values before another apply; do not revive stale approval.
            target['snapshot_current'] = False
            pd.pop('completed',None)
        else:
            # Keep fresh unrelated proposals untouched. Receipt history is
            # independent; a lost card never authorizes invented suggestions.
            pd.setdefault('undo_history', []).append({
                'receipt': receipt, 'field': field, 'state': 'undone',
                'before': json.loads(record['before_value']),
                'after': _display_payload(json.loads(record['after_json']))})
        conn.execute("UPDATE notion_policies SET notion_fields=?,pending_diff=?,change_status='detected' WHERE id=?",(json.dumps(fields),json.dumps(pd),policy_id))
        conn.execute("UPDATE policy_apply_receipts SET state='undone' WHERE id=?",(receipt,))
        conn.commit()
    return {'ok':True}


async def reconcile(policy_id):
    """Read-only Notion reconciliation during an explicit successful sync.

    No PATCH, inferred legacy outcome, deleted receipt or automatic replay.
    Call under the policy maintenance gate. Exact writable values include format.
    """
    with database.get_connection() as conn:
        if not _receipt_table_exists(conn):
            return {'resolved': 0, 'pending': 0}
        schema(conn)
        policy = conn.execute('SELECT page_id FROM notion_policies WHERE id=?', (policy_id,)).fetchone()
        if not policy:
            return {'resolved': 0, 'pending': 0}
        records = [dict(row) for row in conn.execute("SELECT * FROM policy_apply_receipts WHERE page_id=? AND state IN ('prepared','undoing','uncertain')",
                                                   (policy['page_id'],)).fetchall()]
        conn.commit()
    known = [row for row in records if row['operation'] in ('apply', 'undo')]
    if not known:
        return {'resolved': 0, 'pending': len(records)}
    try:
        page = await notion_service.get_page(policy['page_id'])
    except Exception:
        return {'resolved': 0, 'pending': len(records)}
    if not page or page.get('archived') is True or page.get('in_trash') is True:
        return {'resolved': 0, 'pending': len(records)}
    updates = []
    for row in known:
        prop = next((value for value in page.get('properties', {}).values()
                     if value.get('id') == row['property_id']), None)
        if not prop:
            continue
        try:
            before, after = json.loads(row['before_json']), json.loads(row['after_json'])
            # Incomplete rich-text/unsupported values are not a proven third value.
            writable(prop)
            if matches(prop, after):
                state = 'applied'
            elif matches(prop, before):
                state = 'undone' if row['operation'] == 'undo' else 'failed'
            else:
                state = 'superseded'  # A manual/newer value must never be overwritten.
        except (ValueError, TypeError, KeyError):
            continue
        updates.append((state, row['id'], row['state'], row['operation']))
    resolved = 0
    with database.get_connection() as conn:
        for values in updates:
            changed = conn.execute('UPDATE policy_apply_receipts SET state=? WHERE id=? AND state=? AND operation=?', values)
            resolved += changed.rowcount
        conn.commit()
    return {'resolved': resolved, 'pending': len(records) - resolved}

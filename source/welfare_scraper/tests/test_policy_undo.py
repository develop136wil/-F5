import copy
import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
from services import policy_undo as undo, policy_sync as ps


def prop(value):
    return {'id':'stable-property','type':'rich_text','rich_text':[{'type':'text','text':{'content':value},'annotations':{'bold':True,'italic':False,'strikethrough':False,'underline':False,'code':False,'color':'default'}}]}


class UndoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        database.close_all_connections()
        self.patch=patch.object(database,'DB_FILE',str(Path(self.temp.name)/'undo.db'))
        self.patch.start(); database.init_db()
        self.before=prop('적용 전 내용'); self.after=prop('반영한 내용')
        self.page={'properties':{'지원내용':copy.deepcopy(self.after)}}
        self.pd={'intensity':'high','fields':[{'field':'지원내용','old':'적용 전 내용','new':'반영한 내용','level':'high','applied':True}]}
        with database.get_connection() as conn:
            conn.execute("INSERT INTO notion_policies (id,db_key,page_id,notion_fields,pending_diff,change_status) VALUES (1,'test','page',?,?,'detected')",(json.dumps({'support_detail':'반영한 내용'}),json.dumps(self.pd)))
            conn.commit()
        target={'page_id':'page','properties':{'지원내용':undo.writable(self.after)},'fkey':'support_detail'}
        self.receipt=undo.prepare(1,'지원내용',target,{'properties':{'지원내용':self.before}},'적용 전 내용')
        undo.mark(self.receipt,'applied')
        self.pd['fields'][0]['undo_id']=self.receipt
        with database.get_connection() as conn:
            conn.execute('UPDATE notion_policies SET pending_diff=? WHERE id=1',(json.dumps(self.pd),));conn.commit()

    def tearDown(self):
        database.close_all_connections();self.patch.stop();self.temp.cleanup()

    async def read(self, _): return copy.deepcopy(self.page)
    async def write(self, page, properties):
        self.assertEqual(page,'page'); self.assertEqual(list(properties),['stable-property'])
        self.page={'properties':{'이름이 바뀐 속성':{'id':'stable-property','type':'rich_text',**copy.deepcopy(properties['stable-property'])}}}
        return {'ok':True}

    async def test_undo_restores_exact_format_and_reopens_proposal(self):
        with patch.object(undo.notion_service,'get_page',side_effect=self.read),patch.object(undo.notion_service,'update_page_properties',side_effect=self.write) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertTrue(result['ok']);self.assertEqual(write.call_count,1)
            self.assertTrue(undo.matches(next(iter(self.page['properties'].values())),undo.writable(self.before)))
            duplicate=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertFalse(duplicate['ok']);self.assertEqual(write.call_count,1)
        with database.get_connection() as conn:
            row=conn.execute('SELECT * FROM notion_policies WHERE id=1').fetchone()
            self.assertFalse(json.loads(row['pending_diff'])['fields'][0]['applied'])
            self.assertEqual(json.loads(row['notion_fields'])['support_detail'],'적용 전 내용')

    async def test_manual_change_never_overwritten(self):
        self.page['properties']['지원내용']=prop('사용자가 직접 수정')
        with patch.object(undo.notion_service,'get_page',side_effect=self.read),patch.object(undo.notion_service,'update_page_properties',new_callable=AsyncMock) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertEqual(result['error'],'UNDO_STALE');write.assert_not_called()

    async def test_missing_page_never_overwritten(self):
        with patch.object(undo.notion_service,'get_page',return_value=None),patch.object(undo.notion_service,'update_page_properties',new_callable=AsyncMock) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertFalse(result['ok']);write.assert_not_called()

    async def test_unknown_receipt_never_reads_or_writes_notion(self):
        with patch.object(undo.notion_service,'get_page',new_callable=AsyncMock) as read:
            result=await ps.undo_policy_field(1,'지원내용','unknown')
            self.assertFalse(result['ok']);read.assert_not_called()

    async def test_apply_requires_durable_before_record(self):
        target={'page_id':'page','properties':{'지원내용':undo.writable(self.after)},'fkey':'support_detail','old_display':'적용 전 내용','py_value':'반영한 내용'}
        with patch.object(ps,'_apply_field_sync',return_value=(target,None,self.pd)),patch.object(ps,'_live_field_matches_snapshot',return_value=True),patch.object(undo.notion_service,'extract_page_fields',return_value={'support_detail':'적용 전 내용'}),patch.object(undo.notion_service,'get_page',return_value={'properties':{'지원내용':self.before}}),patch.object(undo,'prepare',side_effect=OSError('test disk full')),patch.object(undo.notion_service,'update_page_properties',new_callable=AsyncMock) as write:
            result=await ps.apply_field_to_notion(1,'지원내용')
            self.assertEqual(result['error'],'UNDO_RECORD_FAILED');write.assert_not_called()

    async def test_undo_post_write_verification_failure_blocks_retry(self):
        with patch.object(undo.notion_service,'get_page',side_effect=[copy.deepcopy(self.page),None]),patch.object(undo.notion_service,'update_page_properties',return_value={'ok':True}) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertEqual(result['error'],'UPDATE_UNCERTAIN')
            self.assertFalse((await ps.undo_policy_field(1,'지원내용',self.receipt))['ok'])
            self.assertEqual(write.call_count,1)

    async def test_cleanup_does_not_destroy_durable_undo_access(self):
        ps.set_status(1,'confirmed')
        with patch.object(undo.notion_service,'get_page',side_effect=self.read), patch.object(undo.notion_service,'update_page_properties',side_effect=self.write) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertTrue(result['ok']);write.assert_awaited_once()
        self.assertEqual(undo.applied_records(page_id='page'), [])

    async def test_uncertain_patch_cannot_be_replayed(self):
        with patch.object(undo.notion_service,'get_page',side_effect=self.read),patch.object(undo.notion_service,'update_page_properties',return_value={'ok':False,'error':'UPDATE_UNCERTAIN'}) as write:
            result=await ps.undo_policy_field(1,'지원내용',self.receipt)
            self.assertEqual(result['error'],'UPDATE_UNCERTAIN')
            self.assertFalse((await ps.undo_policy_field(1,'지원내용',self.receipt))['ok'])
            self.assertEqual(write.call_count,1)

    async def test_missing_card_still_checks_live_value_before_undo(self):
        database.execute_write('UPDATE notion_policies SET pending_diff=NULL WHERE id=1')
        self.page['properties']['지원내용'] = prop('수동으로 수정한 값')
        with patch.object(undo.notion_service, 'get_page', side_effect=self.read), patch.object(undo.notion_service, 'update_page_properties', new_callable=AsyncMock) as write:
            result = await ps.undo_policy_field(1, '지원내용', self.receipt)
        self.assertEqual(result['error'], 'UNDO_STALE')
        write.assert_not_awaited()

    async def test_receipt_for_another_page_never_restores(self):
        database.execute_write("UPDATE notion_policies SET page_id='other-page' WHERE id=1")
        with patch.object(undo.notion_service, 'get_page', new_callable=AsyncMock) as read:
            result = await ps.undo_policy_field(1, '지원내용', self.receipt)
        self.assertEqual(result['error'], 'UNDO_UNAVAILABLE')
        read.assert_not_awaited()

    async def test_prepared_receipt_is_uncertain_and_restores_only_matching_after(self):
        undo.mark(self.receipt, 'prepared')
        listed = undo.applied_records(page_id='page')
        self.assertTrue(listed[0]['needs_verification'])
        self.assertEqual(listed[0]['before_value'], '적용 전 내용')
        self.assertEqual(listed[0]['after_value'], '반영한 내용')
        with patch.object(undo.notion_service, 'get_page', side_effect=self.read), patch.object(undo.notion_service, 'update_page_properties', side_effect=self.write):
            result = await ps.undo_policy_field(1, '지원내용', self.receipt)
        self.assertTrue(result['ok'])

    def test_uncertain_receipts_remain_visible_but_cannot_be_replayed(self):
        for state in ('uncertain', 'undoing'):
            with self.subTest(state=state):
                undo.mark(self.receipt, state)
                listed = undo.applied_records(page_id='page')
                self.assertEqual(listed[0]['state'], state)
                self.assertTrue(listed[0]['needs_verification'])
                self.assertFalse(listed[0]['can_undo'])
                self.assertFalse(listed[0]['undo_available'])
                self.assertTrue(listed[0]['guidance'])

    async def test_older_identical_receipt_cannot_jump_over_newer_apply(self):
        target = {'page_id': 'page', 'properties': {'지원내용': undo.writable(self.after)}, 'fkey': 'support_detail'}
        newer = undo.prepare(1, '지원내용', target, {'properties': {'지원내용': self.before}}, '적용 전 내용')
        undo.mark(newer, 'applied')
        with patch.object(undo.notion_service, 'get_page', new_callable=AsyncMock) as read:
            result = await ps.undo_policy_field(1, '지원내용', self.receipt)
        self.assertEqual(result['error'], 'UNDO_STALE')
        read.assert_not_awaited()

    def test_empty_text_and_tags_are_restorable(self):
        self.assertEqual(undo.writable({'type':'rich_text','rich_text':[]}),{'rich_text':[]})
        self.assertTrue(undo.matches({'multi_select':[{'id':'a','name':'A'}]}, {'multi_select':[{'name':'A'}]}))
        self.assertFalse(undo.matches({'multi_select':[{'id':'b','name':'B'}]}, {'multi_select':[{'name':'A'}]}))

    def test_unsupported_and_truncated_values_fail_closed(self):
        with self.assertRaises(ValueError):undo.writable({'type':'formula','formula':{}})
        with self.assertRaises(ValueError):undo.writable({'type':'rich_text','rich_text':[{'type':'text','text':{'content':'x'}}]*25})

    def test_completed_card_stays_until_cleanup_and_sync_preserves_receipt(self):
        ps._finalize_apply(1,'지원내용',self.pd,'support_detail','반영한 내용')
        ps._drop_manually_resolved_fields(1,{'support_detail':'반영한 내용'})
        with database.get_connection() as conn:
            row=conn.execute('SELECT * FROM notion_policies WHERE id=1').fetchone()
            self.assertEqual(row['change_status'],'detected')
            self.assertTrue(json.loads(row['pending_diff'])['completed'])
            self.assertEqual(json.loads(row['pending_diff'])['fields'][0]['undo_id'],self.receipt)

if __name__=='__main__':unittest.main()

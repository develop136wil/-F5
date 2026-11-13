"""Offline regression for single-field outcomes and receipt-only undo."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from services import policy_sync as ps, policy_undo as undo, notion_service
from services.policy_apply_coverage import standalone_apply_hold


def prop(value, ident='eligibility'):
    return {'id': ident, 'type': 'rich_text', 'rich_text': [
        {'type': 'text', 'text': {'content': value}, 'plain_text': value}]}


class ApplyCoverage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database.close_all_connections()
        self.patcher = patch.object(database, 'DB_FILE', str(Path(self.temp.name) / 'policy.db'))
        self.patcher.start()
        database.init_db()
        ps._POLICY_APPLY_LOCKS.clear()
        self.label = ps._PROP_LABEL['eligibility_detail']
        self.old = '등록장애인에게 할인. 경차와 영업용 차량 제외. 등록장애인 탑승 및 할인카드 제시 필요'
        self.new = '등록장애인에게 할인'
        self.source = self.old
        self.pending = {'proposal_schema_version': 2, 'intensity': 'high',
                        'extraction_meta': {'prompt_version': ps._EXTRACTION_PROMPT_VERSION},
                        'source_diff': {'url': 'https://example.invalid/policy', 'new': self.source},
                        'fields': [{'field': self.label, 'old': self.old, 'new': self.new,
                                    'level': 'high', 'snapshot_current': True},
                                   {'field': ps._PROP_LABEL['support_detail'], 'old': '통행료 할인',
                                    'new': self.old, 'level': 'high', 'snapshot_current': True}]}
        self.current = {'eligibility_detail': self.old, 'support_detail': '통행료 할인'}
        self.page = {'properties': {self.label: prop(self.old),
                                   ps._PROP_LABEL['support_detail']: prop('통행료 할인', 'support')}}
        title_name = next(name for key, name, kind in notion_service.get_policy_props() if key == 'title')
        self.page['properties'][title_name] = {'id': 'title', 'type': 'title',
            'title': [{'type': 'text', 'text': {'content': '합성정책'}, 'plain_text': '합성정책'}]}
        database.execute_write('INSERT INTO notion_policies (id,db_key,page_id,title,notion_fields,pending_diff,source_texts,change_status) VALUES (1,?,?,?,?,?,?,?)',
            ('test', 'page', '합성정책', json.dumps(self.current), json.dumps(self.pending),
             json.dumps({'https://example.invalid/policy': self.source}), 'detected'))

    def tearDown(self):
        database.close_all_connections()
        self.patcher.stop()
        ps._POLICY_APPLY_LOCKS.clear()
        self.temp.cleanup()

    def save(self):
        database.execute_write('UPDATE notion_policies SET notion_fields=?,pending_diff=?,source_texts=? WHERE id=1',
            (json.dumps(self.current), json.dumps(self.pending),
             json.dumps({'https://example.invalid/policy': self.source})))

    def changes(self):
        with patch.object(ps, '_active_configured_keys', return_value=['test']):
            return ps.get_changes(['test'])

    async def test_unapplied_sibling_never_preserves_conditions_even_with_force(self):
        for force in (False, True):
            with patch.object(notion_service, 'get_page', new_callable=AsyncMock) as read, patch.object(notion_service, 'update_page_properties', new_callable=AsyncMock) as write:
                result = await ps.apply_field_to_notion(1, self.label, force=force, force_semantic=force)
            self.assertEqual(result['error'], 'NEEDS_LINKED_REVIEW')
            read.assert_not_awaited()
            write.assert_not_awaited()

    async def test_current_sibling_preserves_and_live_recheck_rejects_manual_removal(self):
        self.current['support_detail'] = self.old
        self.save()
        with patch.object(notion_service, 'get_page', return_value=self.page), patch.object(notion_service, 'update_page_properties', new_callable=AsyncMock) as write:
            result = await ps.apply_field_to_notion(1, self.label)
        self.assertEqual(result['error'], 'NEEDS_LINKED_REVIEW')
        write.assert_not_awaited()

    async def test_current_live_sibling_allows_safe_single_field_apply(self):
        self.current['support_detail'] = self.old
        self.page['properties'][ps._PROP_LABEL['support_detail']] = prop(self.old, 'support')
        self.save()
        with patch.object(notion_service, 'get_page', return_value=self.page), patch.object(notion_service, 'update_page_properties', return_value={'ok': True}) as write:
            result = await ps.apply_field_to_notion(1, self.label)
        self.assertTrue(result['ok'], result)
        write.assert_awaited_once()

    def test_numeric_replacement_requires_exact_latest_source_sentence(self):
        current = {'eligibility_detail': '기준중위소득 80% 이하 가구'}
        self.assertIsNone(standalone_apply_hold('eligibility_detail', '기준중위소득 100% 이하 가구', current, '기준중위소득 100% 이하 가구'))
        self.assertIsNotNone(standalone_apply_hold('eligibility_detail', '기준중위소득 100% 이하 가구', current, '기준중위소득 80% 이하 가구'))
        self.assertIsNotNone(standalone_apply_hold('eligibility_detail', '기준중위소득 100% 이상 가구', current, '기준중위소득 100% 이상 가구'))

    def test_absence_from_source_is_not_a_repeal_certificate(self):
        self.assertIsNotNone(standalone_apply_hold('eligibility_detail', self.new, self.current, self.new))

    def test_condition_words_inside_a_negation_do_not_count_as_preserved(self):
        self.assertIsNotNone(standalone_apply_hold('eligibility_detail', '등록장애인 대상 아님',
            {'eligibility_detail': '등록장애인 대상'}, '등록장애인 대상 아님'))
        self.assertIsNotNone(standalone_apply_hold('eligibility_detail', '등록장애인 대상',
            {'eligibility_detail': '등록장애인 대상. 경차 제외'}, '등록장애인 대상'))

    async def test_missing_snapshot_blocks_even_current_prompt_and_force(self):
        self.pending['fields'][0].pop('snapshot_current')
        self.save()
        with patch.object(notion_service, 'get_page', new_callable=AsyncMock) as read:
            result = await ps.apply_field_to_notion(1, self.label, True, True)
        self.assertFalse(result['ok'])
        self.assertIn('현재 Notion 원본', result['error'])
        read.assert_not_awaited()

    async def test_unresolved_source_hold_blocks_direct_apply_despite_fresh_proposal(self):
        from services import policy_review_store
        policy_review_store.record_hold(1, '합성정책', ['https://example.invalid/policy'],
            self.source, {'code': 'source_read_incomplete'})
        with patch.object(notion_service, 'get_page', new_callable=AsyncMock) as read, patch.object(notion_service, 'update_page_properties', new_callable=AsyncMock) as write:
            result = await ps.apply_field_to_notion(1, self.label, True, True)
        self.assertEqual(result['error'], 'NEEDS_SOURCE_REVIEW')
        read.assert_not_awaited()
        write.assert_not_awaited()

    async def test_unconfirmed_receipt_blocks_new_apply_on_same_page(self):
        target = {'page_id': 'page', 'fkey': 'eligibility_detail', 'properties': {self.label: {'rich_text': [{'type': 'text', 'text': {'content': self.new}}]}}}
        receipt = undo.prepare(1, self.label, target, self.page, self.old)
        for state in ('prepared', 'uncertain', 'undoing'):
            with self.subTest(state=state):
                undo.mark(receipt, state)
                with patch.object(notion_service, 'get_page', new_callable=AsyncMock) as read, patch.object(notion_service, 'update_page_properties', new_callable=AsyncMock) as write:
                    result = await ps.apply_field_to_notion(1, self.label, True, True)
                self.assertEqual(result['error'], 'APPLY_RESULT_UNCERTAIN')
                read.assert_not_awaited()
                write.assert_not_awaited()

    async def test_local_failure_keeps_receipt_and_undo_without_card_link(self):
        self.pending['fields'][0]['new'] = self.old + '. 신청은 주민센터에서 가능'
        self.save()
        original = copy.deepcopy(self.page)
        async def read(_):
            return copy.deepcopy(self.page)
        async def write(_, properties):
            for key, payload in properties.items():
                name = self.label if key in (self.label, 'eligibility') else key
                self.page['properties'][name] = {'id': 'eligibility', 'type': 'rich_text', **payload}
            return {'ok': True}
        with patch.object(notion_service, 'get_page', side_effect=read), patch.object(notion_service, 'update_page_properties', side_effect=write), patch.object(ps, '_finalize_apply', side_effect=OSError('synthetic disk full')):
            result = await ps.apply_field_to_notion(1, self.label)
        self.assertEqual(result['error'], 'LOCAL_SAVE_FAILED')
        receipts = undo.applied_records(page_id='page')
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['state'], 'applied')
        self.assertNotIn('undo_id', database.execute_query('SELECT pending_diff FROM notion_policies WHERE id=1', fetch_one=True)['pending_diff'])
        changes = self.changes()
        self.assertEqual(changes[0]['apply_history'][0]['undo_id'], receipts[0]['id'])
        self.assertFalse(changes[0]['apply_ready'], 'Remote write invalidated the unfinalized before snapshot')
        with patch.object(notion_service, 'get_page', side_effect=read), patch.object(notion_service, 'update_page_properties', side_effect=write):
            restored = await ps.undo_policy_field(1, self.label, receipts[0]['id'])
        self.assertTrue(restored['ok'], restored)
        self.assertTrue(undo.matches(self.page['properties'][self.label], undo.writable(original['properties'][self.label])))

    async def test_receipt_survives_changed_local_policy_id_but_not_changed_page(self):
        target = {'page_id': 'page', 'fkey': 'eligibility_detail', 'properties': {self.label: {'rich_text': [{'type': 'text', 'text': {'content': self.new}}]}}}
        receipt = undo.prepare(1, self.label, target, self.page, self.old)
        undo.mark(receipt, 'applied')
        database.execute_write('UPDATE notion_policies SET id=2,pending_diff=NULL WHERE id=1')
        self.page['properties'][self.label] = prop(self.new)
        async def restore(_, properties):
            self.page['properties'][self.label] = {'id': 'eligibility', 'type': 'rich_text', **properties['eligibility']}
            return {'ok': True}
        with patch.object(notion_service, 'get_page', side_effect=lambda _: copy.deepcopy(self.page)), patch.object(notion_service, 'update_page_properties', side_effect=restore):
            result = await ps.undo_policy_field(2, self.label, receipt)
        self.assertTrue(result['ok'], result)

    def test_receipt_listing_does_not_create_missing_table(self):
        with database.get_connection() as conn:
            conn.execute('DROP TABLE IF EXISTS policy_apply_receipts')
            conn.commit()
        self.assertEqual(undo.applied_records(page_id='page'), [])
        with database.get_connection() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='policy_apply_receipts'").fetchone())

    def test_get_changes_retains_undo_after_empty_or_replaced_pending(self):
        target = {'page_id': 'page', 'fkey': 'eligibility_detail', 'properties': {self.label: {'rich_text': [{'type': 'text', 'text': {'content': self.new}}]}}}
        receipt = undo.prepare(1, self.label, target, self.page, self.old)
        undo.mark(receipt, 'applied')
        for pending, status in ((None, 'none'),
                                (json.dumps({'fields': [{'field': ps._PROP_LABEL['target_income'], 'old': '80%', 'new': '100%'}]}), 'detected')):
            with self.subTest(status=status, pending=pending):
                database.execute_write('UPDATE notion_policies SET pending_diff=?,change_status=? WHERE id=1', (pending, status))
                changes = self.changes()
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]['apply_history'][0]['undo_id'], receipt)
                self.assertFalse(changes[0]['apply_ready'])


if __name__ == '__main__':
    unittest.main()

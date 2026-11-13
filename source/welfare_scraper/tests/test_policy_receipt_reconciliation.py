"""Read-only outcome recovery; no operational DB, live API or automatic PATCH."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import database
from services import notion_service as ns, policy_sync as ps, policy_undo as undo
from tests import test_policy_final_connection_audit as fixtures

live_page = fixtures.live_page


class ReceiptReconciliation(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PolicyConnectionAudit.setUp
    tearDown = fixtures.PolicyConnectionAudit.tearDown
    insert = fixtures.PolicyConnectionAudit.insert

    def prepare(self, *, operation='apply'):
        target, error, _ = ps._apply_field_sync(1, self.label)
        self.assertIsNone(error)
        receipt = undo.prepare(1, self.label, target, live_page(self.fields), self.fields['support_detail'])
        undo.mark(receipt, 'uncertain', operation)
        return receipt

    def state(self, receipt):
        return dict(database.execute_query('SELECT state,operation FROM policy_apply_receipts WHERE id=?',
                                           (receipt,), fetch_one=True))

    async def test_known_operations_resolve_before_after_and_third_without_patch(self):
        for operation in ('apply', 'undo'):
            for value, expected in ((self.fields['support_detail'], 'failed' if operation == 'apply' else 'undone'),
                                    (self.new, 'applied'), ('사용자가 직접 수정한 내용', 'superseded')):
                with self.subTest(operation=operation, expected=expected):
                    receipt = self.prepare(operation=operation)
                    page = live_page({**self.fields, 'support_detail': value})
                    with patch.object(ns, 'get_page', new=AsyncMock(return_value=page)) as read, \
                         patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
                        result = await undo.reconcile(1)
                    self.assertEqual(result, {'resolved': 1, 'pending': 0})
                    self.assertEqual(self.state(receipt), {'state': expected, 'operation': operation})
                    read.assert_awaited_once()
                    write.assert_not_awaited()

    async def test_unknown_legacy_operation_stays_held_even_with_matching_values(self):
        receipt = self.prepare()
        database.execute_write("UPDATE policy_apply_receipts SET operation='unknown' WHERE id=?", (receipt,))
        with patch.object(ns, 'get_page', new=AsyncMock()) as read, \
             patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
            result = await undo.reconcile(1)
        self.assertEqual(result, {'resolved': 0, 'pending': 1})
        self.assertEqual(self.state(receipt)['state'], 'uncertain')
        read.assert_not_awaited()
        write.assert_not_awaited()

    async def test_unreadable_archived_missing_or_truncated_property_keeps_receipt(self):
        receipt = self.prepare()
        truncated = live_page(self.fields)
        prop = truncated['properties'][self.label]
        prop['rich_text'] *= 25
        for page in (None, {'archived': True, **live_page(self.fields)}, {'properties': {}}, truncated):
            with self.subTest(page=bool(page)), \
                 patch.object(ns, 'get_page', new=AsyncMock(return_value=page)), \
                 patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
                self.assertEqual(await undo.reconcile(1), {'resolved': 0, 'pending': 1})
                self.assertEqual(self.state(receipt)['state'], 'uncertain')
                write.assert_not_awaited()

    async def test_get_error_or_cancellation_never_changes_receipt(self):
        receipt = self.prepare()
        with patch.object(ns, 'get_page', new=AsyncMock(side_effect=OSError('offline'))):
            self.assertEqual(await undo.reconcile(1), {'resolved': 0, 'pending': 1})
        with patch.object(ns, 'get_page', new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await undo.reconcile(1)
        self.assertEqual(self.state(receipt)['state'], 'uncertain')

    async def test_successful_explicit_sync_reconciles_but_partial_sync_does_not(self):
        db = {'key': 'audit', 'name': '합성 보관함', 'data_source_id': 'fake-source'}
        page = live_page({**self.fields, 'support_detail': self.new})
        fields = ns.extract_page_fields(page)
        receipt = self.prepare()
        with patch.object(ns, 'get_policy_dbs', return_value=[db]), \
             patch.object(ns, 'get_credentials', return_value=('mock-token', '')), \
             patch.object(ns, 'resolve_target', new=AsyncMock(return_value={'ok': True, 'id': 'fake-source', 'kind': 'data_source', 'version': ns.DS_VERSION})), \
             patch.object(ns, 'get_policy_source_schema', new=AsyncMock(return_value={'ok': True, 'schema': {}})), \
             patch.object(ns, 'query_policy_source_pages', new=AsyncMock(return_value={'ok': False, 'error': 'offline'})) as query, \
             patch.object(ns, 'get_page', new=AsyncMock(return_value=page)) as read, \
             patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
            failed = await ps.sync_policies(['audit'], defer_embeddings=True)
            self.assertFalse(failed['ok'])
            read.assert_not_awaited()
            self.assertEqual(self.state(receipt)['state'], 'uncertain')
            query.return_value = {'ok': True, 'pages': [fields]}
            synced = await ps.sync_policies(['audit'], defer_embeddings=True)
        self.assertTrue(synced['ok'], synced)
        self.assertEqual(synced['reconciled'], 1)
        self.assertEqual(synced['verification_pending'], 0)
        self.assertEqual(self.state(receipt)['state'], 'applied')
        write.assert_not_awaited()

    async def test_recovered_applied_receipt_retains_exact_undo(self):
        receipt = self.prepare()
        current = live_page({**self.fields, 'support_detail': self.new})
        async def write_remote(_page, values):
            for identity, value in values.items():
                prop = next(p for p in current['properties'].values() if p['id'] == identity)
                prop.update(copy.deepcopy(value))
            return {'ok': True}
        with patch.object(ns, 'get_page', new=AsyncMock(side_effect=lambda *_: copy.deepcopy(current))), \
             patch.object(ns, 'update_page_properties', new=AsyncMock(side_effect=write_remote)) as write:
            await undo.reconcile(1)
            write.assert_not_awaited()
            result = await ps.undo_policy_field(1, self.label, receipt)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.state(receipt)['state'], 'undone')
        write.assert_awaited_once()

"""Real restore/undo boundary in temporary DBs; all Notion calls replaced."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import api_server
import database
from services import policy_undo

NAMES = ('policy_apply_receipts', 'POLICY_APPLY_RECEIPTS', 'Policy_Apply_Receipts')


@contextmanager
def restore_case(name, state, *, malformed=False, missing=False):
    database.close_all_connections(strict=True)
    with tempfile.TemporaryDirectory() as folder:
        live, incoming, safety = [Path(folder) / (name + '.db') for name in ('live', 'incoming', 'safety')]
        with patch.object(database, 'DB_FILE', str(live)):
            try:
                database.init_db()
                with database.get_connection() as db:
                    db.execute('CREATE TABLE restore_marker(value TEXT)')
                    db.execute("INSERT INTO restore_marker VALUES ('original')")
                    db.commit()
                database.backup_to(str(incoming))
                with closing(sqlite3.connect(incoming)) as db, db:
                    db.execute("UPDATE restore_marker SET value='imported'")
                    db.execute("INSERT INTO notion_policies(id,db_key,page_id,notion_fields,pending_diff) VALUES (1,'test','page','{}','{}')")
                    if not missing:
                        policy_undo.schema(db)
                        for receipt, receipt_state in [('receipt', state), ('other', 'uncertain'), ('done', 'undone')]:
                            db.execute('INSERT INTO policy_apply_receipts(id,policy_id,page_id,field,fkey,property_id,before_json,after_json,before_value,state,operation) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                       (receipt, 1, 'page', 'support', 'support_detail', receipt,
                                        json.dumps({'number': 1}), json.dumps({'number': 2}), '1', receipt_state, 'apply'))
                        if malformed:
                            db.execute('ALTER TABLE policy_apply_receipts RENAME COLUMN state TO missing_state')
                        if name != NAMES[0]:
                            db.execute('ALTER TABLE policy_apply_receipts RENAME TO receipts_temporary')
                            db.execute('ALTER TABLE receipts_temporary RENAME TO "' + name + '"')
                assert api_server._validate_restore_candidate(str(incoming)) is None
                yield live, incoming, safety
            finally:
                database.close_all_connections(strict=True)


class RestoredReceiptCase(unittest.IsolatedAsyncioTestCase):
    async def test_actual_restore_all_names_and_states_blocks_notion_reads_and_writes(self):
        for name in NAMES:
            for state in ('applied', 'prepared'):
                with self.subTest(name=name, state=state), restore_case(name, state) as (live, incoming, safety):
                    with patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock()) as read, \
                         patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                        api_server._commit_database_restore(str(incoming), str(live), str(safety))
                        result = await policy_undo.undo(1, 'support', 'receipt')
                        self.assertEqual(result['error'], 'UNDO_UNAVAILABLE')
                        self.assertEqual(policy_undo.applied_records(page_id='page'), [])
                        self.assertEqual(await policy_undo.reconcile(1), {'resolved': 0, 'pending': 0})
                        read.assert_not_called()
                        write.assert_not_called()
                    with database.get_connection() as db:
                        self.assertEqual({r[0] for r in db.execute('SELECT state FROM policy_apply_receipts')}, {'restored'})
                        self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'imported')

    async def test_missing_state_column_aborts_restore_and_recovers_original_database(self):
        # A real SQLite UPDATE failure, not a mocked mark_restored_history error.
        for name in NAMES:
            with self.subTest(name=name), restore_case(name, 'applied', malformed=True) as (live, incoming, safety):
                with patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock()) as read, \
                     patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                    with self.assertRaises(sqlite3.OperationalError):
                        api_server._commit_database_restore(str(incoming), str(live), str(safety))
                    read.assert_not_called()
                    write.assert_not_called()
                with database.get_connection() as db:
                    self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'original')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM notion_policies').fetchone()[0], 0)
                    self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (name,)).fetchone())

    async def test_missing_receipt_table_is_valid_backup(self):
        with restore_case(NAMES[0], 'applied', missing=True) as (live, incoming, safety):
            with patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock()) as read, \
                 patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                api_server._commit_database_restore(str(incoming), str(live), str(safety))
                result = await policy_undo.undo(1, 'support', 'receipt')
                self.assertEqual(result['error'], 'UNDO_UNAVAILABLE')
                read.assert_not_called()
                write.assert_not_called()
            with database.get_connection() as db:
                self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'imported')

    async def test_invalidation_noop_is_not_reported_as_success(self):
        with restore_case(NAMES[2], 'applied') as (live, incoming, safety):
            # Simulate a no-op write after the normal imported-trigger removal.
            original = api_server._discard_imported_trigger_code
            def inject_noop(path):
                original(path)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("CREATE TRIGGER ignore_invalidation BEFORE UPDATE OF state ON policy_apply_receipts BEGIN SELECT RAISE(IGNORE); END")
            with patch.object(api_server, '_discard_imported_trigger_code', side_effect=inject_noop), \
                 patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock()) as read, \
                 patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                with self.assertRaisesRegex(ValueError, '복원 이력'):
                    api_server._commit_database_restore(str(incoming), str(live), str(safety))
                read.assert_not_called()
                write.assert_not_called()
            with database.get_connection() as db:
                self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'original')

    async def test_unrestored_local_receipts_remain_readable_and_undoable(self):
        # No restoration: name spelling alone must not remove legitimate undo.
        for name in NAMES:
            for state in ('applied', 'prepared'):
                with self.subTest(name=name, state=state), restore_case(name, state) as (_live, incoming, _safety):
                    database.close_all_connections(strict=True)
                    with patch.object(database, 'DB_FILE', str(incoming)):
                        try:
                            records = policy_undo.applied_records(page_id='page')
                            self.assertTrue(any(r['id'] == 'receipt' and r['can_undo'] for r in records))
                            current = {'number': 2}
                            async def get_page(_page):
                                return {'properties': {'support': {'id': 'receipt', 'type': 'number', **current}}}
                            async def update(_page, props):
                                current.update(props['receipt'])
                                return {'ok': True}
                            with patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock(side_effect=get_page)) as read, \
                                 patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock(side_effect=update)) as write:
                                result = await policy_undo.undo(1, 'support', 'receipt')
                                self.assertTrue(result['ok'])
                                self.assertEqual(read.await_count, 2)
                                self.assertEqual(write.await_count, 1)
                        finally:
                            database.close_all_connections(strict=True)


if __name__ == '__main__':
    unittest.main()

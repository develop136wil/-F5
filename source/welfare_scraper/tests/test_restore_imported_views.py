"""Imported SQL objects cannot provide restored undo authority or remote reads."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import api_server
import database
from services import policy_undo
from tests.test_restore_receipt_case import NAMES, restore_case


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def make_receipt_view(path, name, with_rowid=True):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('ALTER TABLE policy_apply_receipts RENAME TO receipt_data')
        projection = 'rowid AS rowid, *' if with_rowid else '*'
        db.execute('CREATE VIEW ' + quote(name) + ' AS SELECT ' + projection + ' FROM receipt_data')


class ImportedViews(unittest.IsolatedAsyncioTestCase):
    async def test_actual_restore_views_never_reach_notion(self):
        for name in NAMES:
            for state in ('applied', 'prepared'):
                for with_rowid in (True, False):
                    with self.subTest(name=name, state=state, rowid=with_rowid), \
                         restore_case(NAMES[0], state) as (live, incoming, safety):
                        make_receipt_view(incoming, name, with_rowid)
                        self.assertIsNone(api_server._validate_restore_candidate(str(incoming)))
                        async def get_page(_page):
                            return {'properties': {'support': {'id': 'receipt', 'type': 'number', 'number': 2}}}
                        with patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock(side_effect=get_page)) as read, \
                             patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                            api_server._commit_database_restore(str(incoming), str(live), str(safety))
                            result = await policy_undo.undo(1, 'support', 'receipt')
                            self.assertEqual(result['error'], 'UNDO_UNAVAILABLE')
                            self.assertEqual(policy_undo.applied_records(page_id='page'), [])
                            self.assertEqual(await policy_undo.reconcile(1), {'resolved': 0, 'pending': 0})
                            read.assert_not_called()
                            write.assert_not_called()
                        with database.get_connection() as db:
                            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0], 0)
                            # Unrecognized table data is preserved, but cannot authorize undo.
                            self.assertEqual(db.execute("SELECT state FROM receipt_data WHERE id='receipt'").fetchone()[0], state)
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM receipt_data').fetchone()[0], 3)
                            self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'imported')

    async def test_quoted_and_dependent_views_removed_while_table_data_survives(self):
        with restore_case(NAMES[0], 'applied') as (live, incoming, safety):
            with closing(sqlite3.connect(incoming)) as db, db:
                db.execute('CREATE VIEW "view""one" AS SELECT value FROM restore_marker')
                db.execute('CREATE VIEW "view two" AS SELECT * FROM "view""one"')
                db.execute('CREATE TRIGGER "trigger""one" INSTEAD OF UPDATE ON "view two" BEGIN UPDATE restore_marker SET value=NEW.value; END')
            api_server._commit_database_restore(str(incoming), str(live), str(safety))
            with database.get_connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?", ('trigger"one',)).fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'imported')
                self.assertEqual({r[0] for r in db.execute('SELECT state FROM policy_apply_receipts')}, {'restored'})

    async def test_view_drop_denied_aborts_restore_and_recovers_original(self):
        with restore_case(NAMES[0], 'applied') as (live, incoming, safety):
            make_receipt_view(incoming, NAMES[2])
            original_discard = api_server._discard_imported_trigger_code
            original_connect = sqlite3.connect
            def connect_with_denial(*args, **kwargs):
                db = original_connect(*args, **kwargs)
                db.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_VIEW else sqlite3.SQLITE_OK)
                return db
            def discard_with_denial(path):
                with patch.object(sqlite3, 'connect', side_effect=connect_with_denial):
                    return original_discard(path)
            with patch.object(api_server, '_discard_imported_trigger_code', side_effect=discard_with_denial), \
                 patch.object(policy_undo.notion_service, 'get_page', new=AsyncMock()) as read, \
                 patch.object(policy_undo.notion_service, 'update_page_properties', new=AsyncMock()) as write:
                with self.assertRaises(sqlite3.DatabaseError):
                    api_server._commit_database_restore(str(incoming), str(live), str(safety))
                read.assert_not_called()
                write.assert_not_called()
            with database.get_connection() as db:
                self.assertEqual(db.execute('SELECT value FROM restore_marker').fetchone()[0], 'original')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM notion_policies').fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0], 0)

    async def test_discard_sql_objects_is_atomic_on_ddl_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'objects.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE data(value TEXT)')
                db.execute("INSERT INTO data VALUES ('retained')")
                db.execute('CREATE TRIGGER guard AFTER INSERT ON data BEGIN SELECT 1; END')
                db.execute('CREATE VIEW display AS SELECT * FROM data')
            original_connect = sqlite3.connect
            def connect_with_denial(*args, **kwargs):
                db = original_connect(*args, **kwargs)
                db.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_VIEW else sqlite3.SQLITE_OK)
                return db
            with patch.object(sqlite3, 'connect', side_effect=connect_with_denial):
                with self.assertRaises(sqlite3.DatabaseError):
                    api_server._discard_imported_trigger_code(str(path))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT value FROM data').fetchone()[0], 'retained')
                self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger')")}, {'guard', 'display'})

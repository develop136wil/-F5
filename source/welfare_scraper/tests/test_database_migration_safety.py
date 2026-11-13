"""Legacy migration exercises synthetic databases only, including active WAL."""
import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import database


class DatabaseMigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / 'legacy'
        self.install.mkdir()
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.source = self.install / 'welfare.db'
        self.target = self.profile / 'welfare.db'
        self.patches = [patch.object(database, 'DB_FILE', str(self.target)),
                        patch.object(database, '__file__', str(self.install / 'database.py')),
                        patch.object(database, '_local', threading.local()),
                        patch.object(database, '_all_conns', set())]
        for item in self.patches:
            item.start()

    def tearDown(self):
        database.close_all_connections(strict=True)
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def create_source(self):
        with closing(sqlite3.connect(self.source)) as source:
            source.execute('CREATE TABLE marker (value TEXT)')
            source.execute("INSERT INTO marker VALUES ('original')")
            source.commit()

    @staticmethod
    def rows(path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute('SELECT value FROM marker').fetchall()

    def assert_no_candidate(self):
        self.assertEqual(list(self.profile.glob('welfare_migration_*')), [])

    def test_first_install_without_legacy_still_initializes(self):
        database.migrate_db()
        database.init_db()
        with database.get_connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 0)

    def test_wal_commits_preserved_and_uncommitted_rows_excluded(self):
        with closing(sqlite3.connect(self.source)) as source:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute('CREATE TABLE marker (value TEXT)')
            source.commit()
            source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            source.execute("INSERT INTO marker VALUES ('committed-in-wal')")
            source.commit()
            source.execute("INSERT INTO marker VALUES ('uncommitted')")
            database.migrate_db()
            self.assertEqual(self.rows(self.target), [('committed-in-wal',)])
            self.assertEqual(source.execute('SELECT COUNT(*) FROM marker').fetchone()[0], 2)
            source.rollback()
            self.assertEqual(source.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.rows(self.source), [('committed-in-wal',)])
        self.assert_no_candidate()

    def test_partial_backup_failure_leaves_target_absent_and_retry_succeeds(self):
        self.create_source()
        def partial(source, candidate):
            Path(candidate).write_bytes(b'incomplete synthetic snapshot')
            raise OSError('synthetic full disk')
        with patch.object(database, '_backup_legacy_database', side_effect=partial):
            with self.assertRaisesRegex(database.DatabaseMigrationError, '원본은 보존'):
                database.migrate_db()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.rows(self.source), [('original',)])
        self.assert_no_candidate()
        database.migrate_db()
        self.assertEqual(self.rows(self.target), [('original',)])

    def test_corrupt_source_never_becomes_a_new_empty_database(self):
        self.source.write_bytes(b'synthetic corrupt database')
        with self.assertRaises(database.DatabaseMigrationError):
            database.migrate_db()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), b'synthetic corrupt database')
        self.assert_no_candidate()

    def test_cancelled_backup_cleans_candidate_and_preserves_original(self):
        self.create_source()
        for error in (KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(kind=type(error).__name__):
                with patch.object(database, '_backup_legacy_database', side_effect=error):
                    with self.assertRaises(type(error)):
                        database.migrate_db()
                self.assertFalse(self.target.exists())
                self.assertEqual(self.rows(self.source), [('original',)])
                self.assert_no_candidate()

    def test_existing_target_is_not_replaced(self):
        self.create_source()
        self.target.write_bytes(b'existing target fixture')
        with patch.object(database, '_backup_legacy_database') as backup:
            database.migrate_db()
        backup.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'existing target fixture')

    def test_publish_failure_preserves_source_and_leaves_target_absent(self):
        self.create_source()
        with patch.object(database, '_publish_migrated_database', side_effect=PermissionError('synthetic denied folder')):
            with self.assertRaises(database.DatabaseMigrationError):
                database.migrate_db()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.rows(self.source), [('original',)])
        self.assert_no_candidate()

    def test_concurrent_target_is_not_overwritten_at_publish(self):
        self.create_source()
        publish = database._publish_migrated_database
        def race(candidate, target):
            self.assertFalse(self.target.exists())
            Path(target).write_bytes(b'another startup completed')
            publish(candidate, target)
        with patch.object(database, '_publish_migrated_database', side_effect=race):
            database.migrate_db()
        self.assertEqual(self.target.read_bytes(), b'another startup completed')
        self.assertEqual(self.rows(self.source), [('original',)])
        self.assert_no_candidate()

    def test_backup_time_budget_is_bounded(self):
        self.create_source()
        candidate = self.profile / 'timeout.db'
        with self.assertRaises(TimeoutError):
            database._backup_legacy_database(str(self.source), str(candidate), timeout=-1)
        self.assertEqual(self.rows(self.source), [('original',)])

    def test_import_stops_before_init_after_migration_failure(self):
        # Execute the real module in a synthetic install/profile layout. No
        # subprocess or operational DB is needed to verify import sequencing.
        self.source.write_bytes(b'synthetic corrupt database')
        module_path = Path(__file__).resolve().parents[1] / 'database.py'
        source_code = module_path.read_text(encoding='utf-8')
        namespace = {'__file__': str(self.install / 'database.py'),
                     '__name__': 'migration_startup_fixture'}
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.profile)}), \
             patch('services.logger.setup_logger', return_value=database.logger):
            with self.assertRaisesRegex(RuntimeError, '시작을 중단'):
                exec(compile(source_code, str(self.install / 'database.py'), 'exec'), namespace)
        self.assertFalse((self.profile / 'welfare-scraper' / 'welfare.db').exists())
        self.assertNotIn('init_db', namespace)
        self.assertEqual(self.source.read_bytes(), b'synthetic corrupt database')

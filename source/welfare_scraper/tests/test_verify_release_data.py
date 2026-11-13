from contextlib import closing, redirect_stdout
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.verify_release_data import main, verify


class ReleaseDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.current = Path(self.temp.name) / 'current'
        self.backup = Path(self.temp.name) / 'backup'
        self.current.mkdir()
        with closing(sqlite3.connect(self.current / 'welfare.db')) as db:
            db.execute('CREATE TABLE policy (value TEXT)')
            db.execute("INSERT INTO policy VALUES ('private fixture')")
            db.commit()
        shutil.copytree(self.current, self.backup)

    def test_unchanged_and_absent_optional_files_pass_without_writes(self):
        before = {p.name:p.read_bytes() for p in self.current.iterdir()}
        self.assertTrue(all(verify(self.current, self.backup)['files_unchanged'].values()))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.current.iterdir()})

    def test_new_and_removed_settings_fail(self):
        for root in (self.current, self.backup):
            path = root / 'config.json'
            path.write_text('secret fixture')
            self.assertFalse(verify(self.current, self.backup)['files_unchanged']['config.json'])
            path.unlink()

    def test_same_row_count_changed_content_fails_hash(self):
        with closing(sqlite3.connect(self.current / 'welfare.db')) as db:
            db.execute("UPDATE policy SET value='changed'")
            db.commit()
        result = verify(self.current, self.backup)
        self.assertTrue(result['table_counts_unchanged'])
        self.assertFalse(result['files_unchanged']['welfare.db'])

    def test_missing_db_is_not_created_and_corrupt_db_fails(self):
        path = self.current / 'welfare.db'
        path.unlink()
        self.assertEqual(verify(self.current, self.backup)['integrity'], 'unreadable')
        self.assertFalse(path.exists())
        path.write_bytes(b'not a database')
        self.assertEqual(verify(self.current, self.backup)['integrity'], 'unreadable')

    def test_cli_exit_and_no_sensitive_output(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main([str(self.current), str(self.backup)]), 0)
            (self.current / '.env').write_text('PRIVATE_API_KEY=secret')
            self.assertEqual(main([str(self.current), str(self.backup)]), 1)
        self.assertNotIn('secret', stream.getvalue())
        self.assertNotIn('private fixture', stream.getvalue())


if __name__ == '__main__':
    unittest.main()

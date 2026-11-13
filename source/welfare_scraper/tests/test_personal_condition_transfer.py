import importlib.util
from pathlib import Path
import sqlite3
import unittest
from contextlib import closing

spec = importlib.util.spec_from_file_location('personal_transfer', Path(__file__).resolve().parents[2] / 'scripts/migrate_personal_news_condition.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class PersonalTransferTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('CREATE TABLE keywords(id INTEGER,keyword TEXT); CREATE TABLE excluded_keywords(id INTEGER,keyword TEXT); CREATE TABLE sites(id INTEGER); CREATE TABLE saved_history(value TEXT); INSERT INTO keywords VALUES(1,"복지"); INSERT INTO excluded_keywords VALUES(1,"채용"); INSERT INTO sites VALUES(4); INSERT INTO saved_history VALUES("keep");')
        transfer.store.ensure_schema(self.conn)

    def test_opt_in_transfer_preserves_inputs_history_and_rejects_second_run(self):
        profile = transfer.make_plan(self.conn, {'CUSTOM_AI_PROMPT': '쉽게 설명해주세요'})
        self.assertEqual(profile.region, '서울특별시 도봉구')
        self.assertEqual(profile.max_age_months, 108)
        self.assertEqual(profile.site_ids, ('4',))
        before = transfer.untouched_digest(self.conn)
        transfer.apply_plan(self.conn, profile)
        self.assertEqual(transfer.defaults.read(self.conn), profile)
        self.assertEqual(before, transfer.untouched_digest(self.conn))
        with self.assertRaises(ValueError):
            transfer.make_plan(self.conn, {})
        with self.assertRaises(transfer.store.ProfileConflict):
            transfer.apply_plan(self.conn, profile)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM news_profile_versions').fetchone()[0], 1)

    def test_saved_profiles_preserved_and_existing_current_never_overwritten(self):
        from dataclasses import replace
        profile = transfer.make_plan(self.conn, {})
        other = replace(profile, profile_id='saved', name='내 조건')
        transfer.store.create_profile(self.conn, other)
        transfer.apply_plan(self.conn, profile)
        self.assertEqual(transfer.store.get_profile(self.conn, 'saved'), other)
        with self.assertRaises(ValueError):
            transfer.make_plan(self.conn, {'CUSTOM_AI_PROMPT': 'different'})

    def test_invalid_or_ambiguous_settings_fail_closed(self):
        for values in ({'TARGET_AGE_MAX_MONTHS':'oops'}, {'TARGET_AGE_MAX_MONTHS':241}, {'CUSTOM_AI_PROMPT':'x'*10001}):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    transfer.make_plan(self.conn, values)
        self.conn.execute('DELETE FROM keywords')
        with self.assertRaises(ValueError):
            transfer.make_plan(self.conn, {})

    def test_saved_condition_summary_uses_actual_target_without_ai_instructions(self):
        from dataclasses import replace
        from services.news_profiles_api import _summary
        profile = replace(transfer.make_plan(self.conn, {}), characteristics=('한부모·조손',), ai_prompt='private instruction')
        result = _summary(profile)
        self.assertEqual(result['characteristics'], ['한부모·조손'])
        self.assertEqual(result['region'], profile.region)
        self.assertEqual(result['max_age_months'], 108)
        self.assertNotIn('ai_prompt', result)
        self.assertNotIn('keywords', result)

    def test_unrelated_write_rolls_back_entire_transfer(self):
        self.conn.execute("CREATE TRIGGER unexpected AFTER INSERT ON news_profiles BEGIN INSERT INTO saved_history VALUES('unexpected'); END")
        profile = transfer.make_plan(self.conn, {})
        with self.assertRaises(RuntimeError):
            transfer.apply_plan(self.conn, profile)
        self.assertIsNone(transfer.defaults.read(self.conn))
        self.assertEqual(self.conn.execute('SELECT * FROM saved_history').fetchall(), [('keep',)])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows offline migration CLI')
    def test_cli_commits_backs_up_and_closes_cleanly(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'installation'
            root.mkdir()
            (root / 'config.json').write_text('{}', encoding='utf-8')
            self.conn.commit()
            with closing(sqlite3.connect(root / 'welfare.db')) as disk:
                self.conn.backup(disk)
            fingerprint = transfer.make_plan(self.conn, {}).fingerprint
            backup = Path(temporary) / 'backup'
            args = ['transfer', str(root), '--apply', '--expected-fingerprint', fingerprint, '--backup', str(backup)]
            with patch.object(transfer.sys, 'argv', args), patch.object(transfer, 'require_stopped'), patch('builtins.print'):
                transfer.main()
            with closing(sqlite3.connect(root / 'welfare.db')) as disk, closing(sqlite3.connect(backup / 'welfare.db')) as saved:
                self.assertIsNotNone(transfer.defaults.read(disk))
                self.assertIsNone(transfer.defaults.read(saved))
                self.assertEqual(transfer.untouched_digest(disk), transfer.untouched_digest(saved))

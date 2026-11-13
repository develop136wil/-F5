"""Repository tests use private SQLite files, never the operating app DB."""
from dataclasses import replace
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import hashlib
import json

from services.news_profile import NewsProfile, capture_legacy_profile
from services import news_profile_store as store


class TestNewsProfileStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'profiles.db'
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('PRAGMA foreign_keys=ON')
        store.ensure_schema(self.conn)
        self.profile = NewsProfile('custom', 1, '노년', '서울', min_age_months=780)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def legacy(self):
        return capture_legacy_profile(region='도봉', max_age_months=108,
                                      keywords=['영유아'], excluded_keywords=['채용'],
                                      site_ids=['s1'], ai_prompt='기존 지침',
                                      semantic_text='기존 문장', relevance_enabled=True)

    def test_schema_idempotent_does_not_touch_existing_rows(self):
        self.conn.execute('CREATE TABLE welfare_items (id INTEGER PRIMARY KEY, title TEXT)')
        self.conn.execute("INSERT INTO welfare_items VALUES (1, '기존 정책')")
        self.conn.commit()
        store.ensure_schema(self.conn)
        store.ensure_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM welfare_items').fetchall(), [(1, '기존 정책')])
        self.assertEqual(store.list_profiles(self.conn), [])

    def test_create_persists_across_reopen(self):
        store.create_profile(self.conn, self.profile)
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.assertEqual(store.get_profile(self.conn, 'custom'), self.profile)

    def test_schema_one_profile_without_interest_topics_remains_readable(self):
        legacy_snapshot = self.profile.snapshot()
        legacy_snapshot['schema_version'] = 1
        legacy_snapshot.pop('interest_topics')
        encoded = json.dumps(legacy_snapshot, ensure_ascii=False)
        digest = hashlib.sha256(json.dumps(legacy_snapshot, sort_keys=True, ensure_ascii=False,
                                separators=(',', ':')).encode('utf-8')).hexdigest()
        self.conn.execute("INSERT INTO news_profiles(profile_id,current_revision) VALUES ('custom',1)")
        self.conn.execute("INSERT INTO news_profile_versions(profile_id,revision,snapshot_json,fingerprint) VALUES ('custom',1,?,?)",
                          (encoded, digest))
        self.assertEqual(store.get_profile(self.conn, 'custom'), self.profile)

    def test_duplicate_create_never_overwrites(self):
        store.create_profile(self.conn, self.profile)
        with self.assertRaises(store.ProfileConflict):
            store.create_profile(self.conn, replace(self.profile, name='덮어쓰기'))
        self.assertEqual(store.get_profile(self.conn, 'custom'), self.profile)

    def test_history_and_optimistic_revision_conflict(self):
        store.create_profile(self.conn, self.profile)
        next_version = replace(self.profile, revision=2, name='수정')
        store.save_revision(self.conn, next_version, expected_revision=1)
        with self.assertRaises(store.ProfileConflict):
            store.save_revision(self.conn, replace(next_version, name='오래된 화면'), expected_revision=1)
        self.assertEqual(store.get_profile(self.conn, 'custom'), next_version)
        self.assertEqual(store.get_profile(self.conn, 'custom', 1), self.profile)

    def test_separate_connections_stale_editor_is_rejected(self):
        store.create_profile(self.conn, self.profile)
        with closing(sqlite3.connect(self.path)) as editor:
            old = store.get_profile(editor, 'custom')
            store.save_revision(self.conn, replace(self.profile, revision=2), expected_revision=1)
            with self.assertRaises(store.ProfileConflict):
                store.save_revision(editor, replace(old, revision=2, name='경합'), expected_revision=1)

    def test_version_insert_failure_rolls_back_current_pointer(self):
        store.create_profile(self.conn, self.profile)
        self.conn.execute('''CREATE TRIGGER fail_version BEFORE INSERT ON news_profile_versions
            BEGIN SELECT RAISE(ABORT, 'simulated storage error'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            store.save_revision(self.conn, replace(self.profile, revision=2), expected_revision=1)
        self.assertEqual(store.get_profile(self.conn, 'custom'), self.profile)

    def test_create_failure_does_not_leave_empty_header(self):
        self.conn.execute('''CREATE TRIGGER fail_version BEFORE INSERT ON news_profile_versions
            BEGIN SELECT RAISE(ABORT, 'simulated storage error'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_profile(self.conn, self.profile)
        self.assertEqual(store.list_profiles(self.conn), [])

    def test_caller_transaction_is_not_committed(self):
        self.conn.execute('BEGIN')
        store.create_profile(self.conn, self.profile)
        self.conn.rollback()
        self.assertEqual(store.list_profiles(self.conn), [])

    def test_legacy_first_capture_is_idempotent_and_protected(self):
        original = self.legacy()
        store.initialize_legacy(self.conn, original)
        self.assertEqual(store.initialize_legacy(self.conn, replace(original, keywords=['別'])), original)
        with self.assertRaises(store.ProtectedProfile):
            store.save_revision(self.conn, replace(original, revision=2), expected_revision=1)
        with self.assertRaises(store.ProtectedProfile):
            store.archive_profile(self.conn, original.profile_id)
        with self.assertRaises(store.ProtectedProfile):
            store.create_profile(self.conn, original)

    def test_clone_is_editable_without_changing_original(self):
        original = store.initialize_legacy(self.conn, self.legacy())
        clone = store.clone_profile(self.conn, original.profile_id, new_id='copy', name='복사본')
        store.save_revision(self.conn, replace(clone, revision=2, keywords=['수정']), expected_revision=1)
        self.assertEqual(store.get_profile(self.conn, original.profile_id), original)

    def test_archive_preserves_versions_but_rejects_edits(self):
        store.create_profile(self.conn, self.profile)
        store.archive_profile(self.conn, 'custom')
        store.archive_profile(self.conn, 'custom')
        self.assertEqual(store.list_profiles(self.conn), [])
        self.assertEqual(store.list_profiles(self.conn, include_archived=True), [(self.profile, True)])
        self.assertEqual(store.get_profile(self.conn, 'custom', 1), self.profile)
        with self.assertRaises(store.ProfileConflict):
            store.save_revision(self.conn, replace(self.profile, revision=2), expected_revision=1)

    def test_tampered_snapshot_is_rejected(self):
        store.create_profile(self.conn, self.profile)
        self.conn.execute("UPDATE news_profile_versions SET fingerprint='wrong'")
        with self.assertRaisesRegex(ValueError, 'integrity'):
            store.get_profile(self.conn, 'custom')

    def test_revision_jump_is_rejected(self):
        store.create_profile(self.conn, self.profile)
        with self.assertRaises(ValueError):
            store.save_revision(self.conn, replace(self.profile, revision=3), expected_revision=1)
        self.assertEqual(store.get_profile(self.conn, 'custom'), self.profile)

    def prepare(self):
        profile = replace(self.profile, site_ids=('7',))
        store.create_profile(self.conn, profile)
        sites = [{'id': 7, 'name': '테스트', 'url': 'https://example.invalid',
                  'selectors': {'title': ['.title']}, 'max_pages': 3,
                  'api_key': 'must-not-persist'}]
        run_id = store.prepare_run(self.conn, 'custom', expected_revision=1, sites=sites)
        return profile, sites, run_id

    def test_run_freezes_nested_sites_and_profile(self):
        profile, sites, run_id = self.prepare()
        sites[0]['selectors']['title'].append('.changed')
        store.save_revision(self.conn, replace(profile, revision=2, keywords=['수정']), expected_revision=1)
        snapshot = store.get_run_snapshot(self.conn, run_id)
        self.assertEqual(snapshot['profile'], profile.snapshot())
        self.assertEqual(snapshot['sites'][0]['selectors'], {'title': ['.title']})
        self.assertNotIn('api_key', snapshot['sites'][0])
        snapshot['sites'][0]['selectors']['title'].append('.returned-copy')
        self.assertEqual(store.get_run_snapshot(self.conn, run_id)['sites'][0]['selectors'],
                         {'title': ['.title']})

    def test_run_survives_archive_and_reopen(self):
        _, _, run_id = self.prepare()
        before = store.get_run_snapshot(self.conn, run_id)
        store.archive_profile(self.conn, 'custom')
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.assertEqual(store.get_run_snapshot(self.conn, run_id), before)

    def test_run_rejects_missing_or_duplicate_sites(self):
        profile, sites, _ = self.prepare()
        for supplied in ([], sites + sites):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                store.prepare_run(self.conn, profile.profile_id, expected_revision=1, sites=supplied)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_snapshots').fetchone()[0], 1)

    def test_run_rejects_stale_and_archived_profile(self):
        profile, sites, _ = self.prepare()
        store.save_revision(self.conn, replace(profile, revision=2), expected_revision=1)
        with self.assertRaises(store.ProfileConflict):
            store.prepare_run(self.conn, 'custom', expected_revision=1, sites=sites)
        store.archive_profile(self.conn, 'custom')
        with self.assertRaises(store.ProfileConflict):
            store.prepare_run(self.conn, 'custom', expected_revision=2, sites=sites)

    def test_run_bokjiro_only_and_unsupported_custom(self):
        legacy = replace(self.legacy(), sources=('bokjiro',), site_ids=())
        store.initialize_legacy(self.conn, legacy)
        run_id = store.prepare_run(self.conn, legacy.profile_id, expected_revision=1, sites=[])
        self.assertEqual(store.get_run_snapshot(self.conn, run_id)['sites'], [])
        store.create_profile(self.conn, replace(self.profile, sources=('bokjiro',)))
        with self.assertRaisesRegex(ValueError, '아직 지원'):
            store.prepare_run(self.conn, 'custom', expected_revision=1, sites=[])

    def test_run_freezes_custom_official_bokjiro_mode_without_sites(self):
        profile = replace(self.profile, sources=('bokjiro',), site_ids=(),
                          life_cycles=('노년',), bokjiro_mode='official_tags_v1')
        store.create_profile(self.conn, profile)
        run_id = store.prepare_run(self.conn, 'custom', expected_revision=1, sites=[])
        snapshot = store.get_run_snapshot(self.conn, run_id)
        self.assertEqual(snapshot['sites'], [])
        self.assertEqual(snapshot['profile']['bokjiro_mode'], 'official_tags_v1')
        self.assertEqual(snapshot['profile']['life_cycles'], ['노년'])

    def test_run_insert_rolls_back_with_caller(self):
        _, sites, _ = self.prepare()
        self.conn.execute('BEGIN')
        run_id = store.prepare_run(self.conn, 'custom', expected_revision=1, sites=sites)
        self.conn.rollback()
        with self.assertRaises(KeyError):
            store.get_run_snapshot(self.conn, run_id)

    def test_run_tampering_is_rejected(self):
        _, _, run_id = self.prepare()
        self.conn.execute("UPDATE news_run_snapshots SET snapshot_json='{}'")
        with self.assertRaisesRegex(ValueError, 'integrity'):
            store.get_run_snapshot(self.conn, run_id)

    def test_latest_profile_identity_checks_stored_revision(self):
        store.create_profile(self.conn, self.profile)
        other = replace(self.profile, revision=2)
        self.conn.execute('UPDATE news_profile_versions SET snapshot_json=?, fingerprint=?',
                          (json.dumps(other.snapshot()), other.fingerprint))
        with self.assertRaisesRegex(ValueError, 'identity'):
            store.get_profile(self.conn, 'custom')


if __name__ == '__main__':
    unittest.main()

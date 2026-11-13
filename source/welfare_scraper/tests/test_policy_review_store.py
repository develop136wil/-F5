"""Persistent source holds use a disposable DB, never an API/Notion operation."""
import hashlib
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
import database
from services import policy_review_store as store, policy_sync as ps
from tests import test_policy_review_resume as resume

SOURCE, URL, row_for = resume.SOURCE, resume.URL, resume.row_for

REVIEW = {'code': 'source_criteria_review_required', 'note': 'DO NOT STORE RAW NOTE',
          'fields': ['age_detail', 'private']}


class DisposableDatabase:
    def setUp(self):
        database.close_all_connections(strict=True)
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, 'DB_FILE', str(Path(self.temp.name) / 'review.db'))
        self.db_patch.start()
        with database.get_connection() as conn:
            conn.execute('CREATE TABLE notion_policies (id INTEGER PRIMARY KEY, db_key TEXT, title TEXT)')
            conn.executemany('INSERT INTO notion_policies VALUES (?, ?, ?)',
                             [(1, 'medical', '기존 정책'), (2, 'medical', '다른 정책'), (3, 'education', '기관 정책')])
            conn.commit()

    def tearDown(self):
        database.close_all_connections(strict=True)
        self.db_patch.stop()
        self.temp.cleanup()

    def hold(self, policy_id=1, source=SOURCE, urls=None, **kwargs):
        return store.record_hold(policy_id, '교육비', urls or [URL], source, REVIEW, **kwargs)


class ReviewStoreTests(DisposableDatabase, unittest.TestCase):
    def test_read_on_old_database_creates_nothing(self):
        with database.get_connection() as conn:
            before = list(conn.execute('SELECT sql FROM sqlite_master'))
            changes = conn.total_changes
            self.assertEqual(store.list_unresolved(['medical']), {'items': [], 'total': 0, 'truncated': False})
            self.assertEqual(list(conn.execute('SELECT sql FROM sqlite_master')), before)
            self.assertEqual(conn.total_changes, changes)

    def test_restart_preserves_holds_and_policy_rows(self):
        self.hold()
        before = store.list_unresolved(['medical'])
        database.close_all_connections(strict=True)
        importlib.reload(store)
        self.assertEqual(store.list_unresolved(['medical']), before)
        with database.get_connection() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM notion_policies ORDER BY id')],
                             [(1, 'medical', '기존 정책'), (2, 'medical', '다른 정책'), (3, 'education', '기관 정책')])
        item = before['items'][0]
        self.assertEqual(item['source_hash'], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual(item['fields'], ['age_detail'])
        self.assertNotIn('DO NOT STORE', item['note'])

    def test_repeat_and_source_versions_are_one_latest_row(self):
        with patch.object(store, '_now', return_value='first'):
            self.hold()
        first_id = store.list_unresolved(['medical'])['items'][0]['id']
        with patch.object(store, '_now', return_value='last'):
            self.hold()
            self.assertEqual(store.list_unresolved(['medical'])['items'][0]['id'], first_id)
            for index in range(100):
                self.hold(source=SOURCE + str(index))
        item = store.list_unresolved(['medical'])['items'][0]
        self.assertEqual(item['first_seen_at'], 'first')
        self.assertEqual(item['last_seen_at'], 'last')
        self.assertNotEqual(item['id'], first_id)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM policy_source_reviews').fetchone()[0], 1)

    def test_private_title_url_and_source_are_not_copied(self):
        secret_url = 'https://example.invalid/policy?token=privateCredential123456789'
        store.record_hold(1, '담당자 private@example.com', [secret_url], 'PRIVATE SOURCE BODY', REVIEW)
        item = store.list_unresolved(['medical'])['items'][0]
        self.assertEqual(item['title'], '정책 #1')
        self.assertEqual(item['source_url'], '')
        with database.get_connection() as conn:
            persisted = str([tuple(row) for row in conn.execute('SELECT * FROM policy_source_reviews')])
        for value in ('private@example.com', 'privateCredential', 'PRIVATE SOURCE BODY', 'DO NOT STORE RAW NOTE'):
            self.assertNotIn(value, persisted)

    def test_only_known_preflight_codes_create_records(self):
        self.assertFalse(store.record_hold(1, '정책', [URL], SOURCE, {'code': 'model_abstained'}))
        self.assertFalse(store.record_hold(999, '정책', [URL], SOURCE, REVIEW))
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)

    def test_scope_and_pagination_do_not_resolve_or_hide_remaining(self):
        self.hold(1)
        self.hold(2)
        self.hold(3)
        first = store.list_unresolved(['medical'], 1, 0)
        second = store.list_unresolved(['medical'], 1, 1)
        self.assertEqual(first['total'], 2)
        self.assertTrue(first['truncated'])
        self.assertFalse(second['truncated'])
        self.assertNotEqual(first['items'][0]['policy_id'], second['items'][0]['policy_id'])
        self.assertEqual(store.list_unresolved(['unknown'])['total'], 0)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 2)
        for limit, offset in [(0, 0), (201, 0), (1, -1), (True, 0)]:
            with self.assertRaises(ValueError):
                store.list_unresolved(['medical'], limit, offset)

    def test_subset_or_unrelated_success_cannot_resolve_source_group(self):
        other = 'https://example.invalid/other'
        self.hold(urls=[URL, other])
        self.assertEqual(store.resolve_after_success(1, [URL], '확인된 현재 원문'), 0)
        self.assertEqual(store.resolve_after_success(2, [URL, other], '확인된 현재 원문'), 0)
        self.assertEqual(store.resolve_after_success(1, [URL, other], '확인된 현재 원문'), 1)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
        with database.get_connection() as conn:
            row = conn.execute('SELECT status, resolved_at, resolved_source_hash FROM policy_source_reviews').fetchone()
            self.assertEqual(row['status'], 'resolved')
            self.assertTrue(row['resolved_at'])
            self.assertEqual(row['resolved_source_hash'], hashlib.sha256('확인된 현재 원문'.encode()).hexdigest())

    def test_reopen_resets_incident_time_without_growing_history(self):
        self.hold()
        store.resolve_after_success(1, [URL], '확인된 현재 원문')
        with patch.object(store, '_now', return_value='reopened'):
            self.hold()
        self.assertEqual(store.list_unresolved(['medical'])['items'][0]['first_seen_at'], 'reopened')
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM policy_source_reviews').fetchone()[0], 1)

    def test_baseline_and_current_source_are_distinct(self):
        self.hold()
        self.hold(source_role='baseline')
        result = store.list_unresolved(['medical'])
        self.assertEqual(result['total'], 2)
        self.assertEqual({item['source_role'] for item in result['items']}, {'current', 'baseline'})
        self.assertEqual(len({item['id'] for item in result['items']}), 2)
        baseline = next(item for item in result['items'] if item['source_role'] == 'baseline')
        self.assertIn('이전 비교 원문', baseline['note'])

    def test_policy_delete_cascades_only_metadata(self):
        self.hold()
        self.hold(2)
        with database.get_connection() as conn:
            conn.execute('DELETE FROM notion_policies WHERE id=1')
            conn.commit()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM policy_source_reviews').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notion_policies').fetchone()[0], 2)

    def test_sqlite_backup_includes_reviews_and_foreign_key_contract(self):
        self.hold()
        backup = str(Path(self.temp.name) / 'backup.db')
        database.backup_to(backup)
        with closing(sqlite3.connect(backup)) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(restored.execute('SELECT status FROM policy_source_reviews').fetchone()[0], 'unresolved')
            restored.execute('PRAGMA foreign_keys=ON')
            restored.execute('DELETE FROM notion_policies WHERE id=1')
            self.assertEqual(restored.execute('SELECT COUNT(*) FROM policy_source_reviews').fetchone()[0], 0)
        # The disposable original was not changed by opening the backup.
        self.assertEqual(store.list_unresolved(['medical'])['total'], 1)

    def test_source_group_cap_never_discards_unresolved_work(self):
        other = 'https://example.invalid/other'
        third = 'https://example.invalid/third'
        with patch.object(store, 'MAX_GROUPS_PER_POLICY', 2):
            self.hold()
            self.hold(urls=[other])
            with self.assertRaisesRegex(RuntimeError, '기존 보류'):
                self.hold(urls=[third])
            self.assertEqual(store.list_unresolved(['medical'])['total'], 2)
            store.resolve_after_success(1, [URL], '새 원문')
            self.hold(urls=[third])
            self.assertEqual(store.list_unresolved(['medical'])['total'], 2)
            with database.get_connection() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM policy_source_reviews').fetchone()[0], 2)


class ReviewFlowTests(DisposableDatabase, unittest.IsolatedAsyncioTestCase):
    check = resume.ReviewResume.check

    async def test_repeated_check_is_persistent_without_ai_or_baseline_mutation(self):
        for _ in range(2):
            _, write, call = await self.check(row_for(), SOURCE)
            call.assert_not_awaited()
            self.assertNotIn('last_extracted_fields=', write.await_args.args[0])
        self.assertEqual({item['source_role'] for item in store.list_unresolved(['medical'])['items']},
                         {'current', 'freshness'})
        database.close_all_connections(strict=True)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 2)

    async def test_model_failure_does_not_resolve_existing_hold(self):
        self.hold()
        # Bypass only the fixture's no-rate-call assertion, not product safety.
        with patch.object(ps, 'time') as timer:
            timer.monotonic.return_value = 1.0
            with patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value=None)):
                await self.check(row_for(), '교육비 월 5만원 지원.', response={})
        self.assertEqual({item['source_role'] for item in store.list_unresolved(['medical'])['items']},
                         {'current', 'freshness'})

    async def test_fetch_failure_does_not_resolve_existing_hold(self):
        self.hold()
        _, _, call = await self.check(row_for(), None)
        call.assert_not_awaited()
        self.assertEqual({item['source_role'] for item in store.list_unresolved(['medical'])['items']}, {'current', 'fetch'})

    async def test_historical_hold_recovers_using_current_notion_comparison(self):
        self.hold()
        row = row_for(SOURCE)
        row['last_extracted_fields'] = None
        clear = '교육비 월 5만원 지원.'
        _, write, call = await self.check(row, clear,
            {'support_detail': clear, '_source_evidence': {'support_detail': clear}})
        call.assert_awaited_once()
        self.assertIn('last_extracted_fields=', write.await_args.args[0])
        items = store.list_unresolved(['medical'])['items']
        self.assertEqual(items, [])

    async def test_initial_audit_persists_preflight_before_ai_and_no_completion_write(self):
        row = row_for(SOURCE)
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            await ps.run_initial_audit(['medical'])
        call.assert_not_awaited()
        write.assert_not_awaited()
        self.assertEqual(store.list_unresolved(['medical'])['total'], 1)

    async def test_successful_comparison_resolves_only_its_policy(self):
        self.hold(1)
        self.hold(2)
        clear = '교육비 월 5만원 지원.'
        await self.check(row_for(), clear, {'support_detail': clear, '_source_evidence': {'support_detail': clear}})
        remaining = store.list_unresolved(['medical'])
        self.assertEqual([item['policy_id'] for item in remaining['items']], [2])

    async def test_db_write_failure_does_not_resolve(self):
        self.hold()
        clear = '교육비 월 5만원 지원.'
        row = row_for()
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock(side_effect=sqlite3.OperationalError('fixture save failure'))), \
             patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(clear, clear, False))), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={'support_detail': clear, '_source_evidence': {'support_detail': clear}})), \
             patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])):
            with self.assertRaises(sqlite3.OperationalError):
                await ps.check_policies(['medical'], scraper=object())
        self.assertEqual({item['source_role'] for item in store.list_unresolved(['medical'])['items']},
                         {'current', 'freshness'})

    async def test_hold_storage_failure_keeps_memory_reason_and_reports_failure(self):
        for error in (sqlite3.OperationalError('private database path'), ValueError('private source URL')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(store, 'record_hold', side_effect=error):
                with self.assertRaisesRegex(RuntimeError, '보류 기록을 저장하지 못해') as raised:
                    await self.check(row_for(), SOURCE)
            self.assertNotIn('private', str(raised.exception))
            self.assertFalse(ps.POLICY_STATUS['running'])
            self.assertIn('보류 기록을 저장하지 못해', ps.POLICY_STATUS['message'])
            self.assertNotIn('점검 완료', ps.POLICY_STATUS['message'])
            self.assertEqual(ps.POLICY_STATUS['review_count'], 1)
            self.assertEqual(ps.POLICY_STATUS['ai_required'], 0)

    async def test_resolution_storage_failure_does_not_report_completion(self):
        self.hold()
        clear = '교육비 월 5만원 지원.'
        with patch.object(store, 'resolve_after_success', side_effect=sqlite3.OperationalError('private database path')):
            with self.assertRaisesRegex(RuntimeError, '보류 목록 상태를 갱신하지 못했습니다'):
                await self.check(row_for(), clear, {'support_detail': clear, '_source_evidence': {'support_detail': clear}})
        self.assertEqual({item['source_role'] for item in store.list_unresolved(['medical'])['items']},
                         {'current', 'freshness'})
        self.assertNotIn('점검 완료', ps.POLICY_STATUS['message'])

    async def test_read_api_uses_connected_scope_and_does_not_call_ai(self):
        import api_server
        self.hold(1)
        self.hold(3)
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            result = await api_server.policy_reviews_endpoint('medical', 50, 0)
        call.assert_not_awaited()
        self.assertEqual([item['policy_id'] for item in result['items']], [1])

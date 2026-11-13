"""AI hold persistence in a disposable DB; no live model, Notion or user data."""
from contextlib import closing
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import database
from services import policy_review_store as store
from tests.test_policy_review_store import DisposableDatabase


CODES = (
    'source_missing', 'source_too_long', 'source_criteria_review_required',
    'source_field_incomplete', 'structured_evidence_missing', 'source_schema_invalid',
    'source_conditions_incomplete', 'source_range_invalid', 'source_fact_changed',
    'source_evidence_mismatch', 'source_evidence_boundary_changed',
    'source_presentation_mismatch', 'source_evidence_missing',
)
FIELDS = ('support_detail', 'eligibility_detail', 'age_detail', 'target_income',
          'contact', 'category', 'life_cycle', 'target_characteristics', 'residence',
          'start_age_months', 'end_age_months')
URL = 'https://example.invalid/synthetic-policy'
SOURCE = '합성 정책 원문: 실제 이용자 정보가 아닌 검증용 자료입니다.'
POLICIES = [(1, 'medical', '기존 정책'), (2, 'medical', '다른 정책'), (3, 'education', '기관 정책')]


class AiPolicyReviewStore(DisposableDatabase, unittest.TestCase):
    def put(self, code='source_field_incomplete', *, policy_id=1, fields=FIELDS,
            source=SOURCE, urls=None, role='current', note='RAW NOTE MUST NOT BE STORED'):
        review = {'code': code, 'fields': fields, 'note': note,
                  'diagnostics': {'raw': 'RAW DIAGNOSTIC MUST NOT BE STORED'}}
        return store.record_hold(policy_id, '합성 정책', urls or [URL], source, review, role)

    def policy_rows(self):
        with database.get_connection() as conn:
            return [tuple(row) for row in conn.execute('SELECT * FROM notion_policies ORDER BY id')]

    def review_rows(self):
        with database.get_connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM policy_source_reviews ORDER BY policy_id,source_key')]

    def test_supports_review_is_pure_and_only_recognizes_thirteen_string_codes(self):
        with patch.object(database, 'get_connection', side_effect=AssertionError('supports_review must be pure')):
            for code in CODES:
                with self.subTest(code=code):
                    self.assertIs(store.supports_review({'code': code}), True)
            for review in (None, [], 'source_field_incomplete', {}, {'code': None},
                           {'code': []}, {'code': {}}, {'code': True},
                           {'code': 'model_abstained'}, {'code': 'unknown'},
                           {'code': 'ai_input_review_required'}, {'code': 'network_error'},
                           {'code': 'quota_exceeded'}, {'code': 'source_field_incomplete '}):
                with self.subTest(review=review):
                    self.assertIs(store.supports_review(review), False)

    def test_all_supported_reasons_persist_static_notes_and_allowed_names_only(self):
        for index, code in enumerate(CODES):
            with self.subTest(code=code):
                self.assertTrue(self.put(code, urls=[URL + f'/{index}']))
        output = store.list_unresolved(['medical'])
        self.assertEqual(output['total'], 13)
        self.assertEqual({item['code'] for item in output['items']}, set(CODES))
        for item in output['items']:
            self.assertEqual(item['fields'], sorted(FIELDS))
            self.assertEqual(item['note'], store._NOTES[item['code']])
            self.assertEqual(item['status'], 'unresolved')
            self.assertNotIn('RAW NOTE', item['note'])
            self.assertNotIn('diagnostics', item)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_unknown_abstention_input_and_transport_codes_do_not_create_or_replace_hold(self):
        for code in ('model_abstained', 'unknown', 'ai_input_review_required', 'network_error', 'quota_exceeded'):
            with self.subTest(code=code):
                self.assertFalse(self.put(code))
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
        self.assertTrue(self.put())
        before = self.review_rows()
        for code in ('model_abstained', 'ai_input_review_required', 'network_error', 'quota_exceeded'):
            self.assertFalse(self.put(code, source='통신 오류를 새 분석 원문으로 저장하면 안 됩니다.'))
        self.assertEqual(self.review_rows(), before)
        self.assertFalse(self.put(policy_id=999))
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_only_list_tuple_field_names_are_accepted_without_coercion(self):
        cases = (
            (list(FIELDS), sorted(FIELDS)), (tuple(FIELDS), sorted(FIELDS)),
            (['contact', 'contact', 'unknown', {'contact': 'private'}, 12, True, None], ['contact']),
            ('contact', []), ({'contact': 'private value'}, []), (None, []), (1, []),
            (['CONTACT', ' contact ', 'start_age_months=24'], []),
            (['unknown'] * 100 + ['contact'], []),
            (['contact'] + ['unknown'] * 99 + ['target_income'], ['contact']),
        )
        for fields, expected in cases:
            with self.subTest(fields=repr(fields)[:100]):
                original = copy.deepcopy(fields)
                self.assertTrue(self.put(fields=fields))
                item = store.list_unresolved(['medical'])['items'][0]
                self.assertEqual(item['fields'], expected)
                self.assertEqual(fields, original)
                self.assertEqual(json.loads(self.review_rows()[0]['fields']), expected)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_connection_restart_and_module_reload_preserve_new_hold_codes(self):
        self.put('source_field_incomplete', fields=['contact', 'target_income'])
        self.put('structured_evidence_missing', policy_id=2,
                 fields=['target_characteristics', 'start_age_months'])
        before = store.list_unresolved(['medical'])
        database.close_all_connections(strict=True)
        importlib.reload(store)
        self.assertEqual(store.list_unresolved(['medical']), before)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_same_source_reason_replacement_keeps_one_latest_unresolved_row(self):
        with patch.object(store, '_now', return_value='2026-09-14T01:00:00+00:00'):
            self.put('source_field_incomplete', fields=['contact'])
        first = store.list_unresolved(['medical'])['items'][0]
        with patch.object(store, '_now', return_value='2026-09-14T02:00:00+00:00'):
            self.put('source_field_incomplete', fields=['contact'], note='DIFFERENT RAW NOTE')
            self.assertEqual(store.list_unresolved(['medical'])['items'][0]['id'], first['id'])
            self.put('source_schema_invalid', fields=['category'])
        current = store.list_unresolved(['medical'])['items'][0]
        self.assertEqual(current['code'], 'source_schema_invalid')
        self.assertEqual(current['fields'], ['category'])
        self.assertEqual(current['source_hash'], first['source_hash'])
        self.assertNotEqual(current['id'], first['id'])
        self.assertEqual(current['first_seen_at'], first['first_seen_at'])
        self.assertEqual(current['last_seen_at'], '2026-09-14T02:00:00+00:00')
        self.assertEqual(len(self.review_rows()), 1)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_many_new_source_versions_remain_one_bounded_latest_row(self):
        self.put('source_conditions_incomplete')
        first = store.list_unresolved(['medical'])['items'][0]
        for index in range(25):
            self.put(CODES[3 + index % 10], source=SOURCE + str(index))
        rows = self.review_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_hash'], hashlib.sha256((SOURCE + '24').encode()).hexdigest())
        self.assertEqual(rows[0]['first_seen_at'], first['first_seen_at'])
        self.assertEqual(rows[0]['status'], 'unresolved')

    def test_source_group_order_and_duplicates_keep_same_provenance(self):
        other = URL + '/other'
        self.put('source_evidence_mismatch', urls=[URL, other, URL])
        first = store.list_unresolved(['medical'])['items'][0]
        self.put('source_evidence_mismatch', urls=[other, URL])
        latest = store.list_unresolved(['medical'])['items'][0]
        self.assertEqual(latest['id'], first['id'])
        self.assertEqual(latest['source_count'], 2)
        row = self.review_rows()[0]
        self.assertEqual(set(json.loads(row['source_ids'])),
                         {hashlib.sha256(url.encode()).hexdigest() for url in (URL, other)})
        self.assertEqual(row['source_hash'], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual(len(self.review_rows()), 1)

    def test_current_and_baseline_roles_stay_distinct_and_subset_success_cannot_resolve(self):
        urls = [URL, URL + '/other']
        self.put('source_presentation_mismatch', urls=urls)
        self.put('source_evidence_boundary_changed', urls=urls, role='baseline')
        before = store.list_unresolved(['medical'])
        self.assertEqual(before['total'], 2)
        self.assertEqual({item['source_role'] for item in before['items']}, {'current', 'baseline'})
        self.assertEqual(len({item['id'] for item in before['items']}), 2)
        baseline = next(item for item in before['items'] if item['source_role'] == 'baseline')
        self.assertTrue(baseline['note'].startswith('이전 비교 원문'))
        self.assertEqual(store.resolve_after_success(1, [URL], '새로 확인된 합성 원문'), 0)
        self.assertEqual(store.resolve_after_success(2, urls, '새로 확인된 합성 원문'), 0)
        self.assertEqual(store.list_unresolved(['medical']), before)
        self.assertEqual(store.resolve_after_success(1, urls, '새로 확인된 합성 원문'), 2)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
        self.assertTrue(all(row['resolved_source_hash'] == hashlib.sha256('새로 확인된 합성 원문'.encode()).hexdigest()
                            for row in self.review_rows()))
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_new_ai_hold_reopens_resolved_group_without_growing_history(self):
        self.put('source_schema_invalid')
        self.assertEqual(store.resolve_after_success(1, [URL], '검증된 합성 원문'), 1)
        with patch.object(store, '_now', return_value='2026-09-14T03:00:00+00:00'):
            self.put('source_field_incomplete', fields=['contact'])
        row = self.review_rows()[0]
        self.assertEqual(row['first_seen_at'], '2026-09-14T03:00:00+00:00')
        self.assertEqual(row['status'], 'unresolved')
        self.assertIsNone(row['resolved_at'])
        self.assertIsNone(row['resolved_source_hash'])
        self.assertEqual(len(self.review_rows()), 1)

    def test_pagination_and_connected_database_scope_do_not_mutate_holds(self):
        self.put('source_field_incomplete', policy_id=1)
        self.put('structured_evidence_missing', policy_id=2)
        self.put('source_schema_invalid', policy_id=3)
        before = self.review_rows()
        first = store.list_unresolved(['medical'], limit=1, offset=0)
        second = store.list_unresolved(['medical'], limit=1, offset=1)
        self.assertEqual(first['total'], 2)
        self.assertTrue(first['truncated'])
        self.assertFalse(second['truncated'])
        self.assertEqual({first['items'][0]['policy_id'], second['items'][0]['policy_id']}, {1, 2})
        self.assertEqual([item['policy_id'] for item in store.list_unresolved(['education'])['items']], [3])
        self.assertEqual(store.list_unresolved(['unknown'])['total'], 0)
        self.assertEqual(store.list_unresolved([])['total'], 0)
        self.assertEqual(self.review_rows(), before)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_source_group_cap_never_evicts_unresolved_ai_holds(self):
        with patch.object(store, 'MAX_GROUPS_PER_POLICY', 2):
            self.put('source_field_incomplete')
            self.put('structured_evidence_missing', urls=[URL + '/two'])
            before = self.review_rows()
            with self.assertRaisesRegex(RuntimeError, '기존 보류'):
                self.put('source_schema_invalid', urls=[URL + '/three'])
            self.assertEqual(self.review_rows(), before)
            self.assertEqual(store.resolve_after_success(1, [URL], '새 합성 원문'), 1)
            self.assertTrue(self.put('source_schema_invalid', urls=[URL + '/three']))
            self.assertEqual(len(self.review_rows()), 2)
            self.assertEqual(store.list_unresolved(['medical'])['total'], 2)
            self.assertEqual(self.policy_rows(), POLICIES)

    def test_sqlite_backup_preserves_new_codes_fields_and_foreign_keys(self):
        self.put('source_field_incomplete', fields=['contact', 'target_income'])
        self.put('structured_evidence_missing', policy_id=2, fields=['target_characteristics'])
        before = self.review_rows()
        backup = str(Path(self.temp.name) / 'ai-review-backup.db')
        database.backup_to(backup)
        with closing(sqlite3.connect(backup)) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(), [])
            rows = restored.execute('SELECT policy_id,code,fields,status FROM policy_source_reviews ORDER BY policy_id').fetchall()
            self.assertEqual([(row[0], row[1], json.loads(row[2]), row[3]) for row in rows],
                             [(1, 'source_field_incomplete', ['contact', 'target_income'], 'unresolved'),
                              (2, 'structured_evidence_missing', ['target_characteristics'], 'unresolved')])
            restored.execute('PRAGMA foreign_keys=ON')
            restored.execute('DELETE FROM notion_policies WHERE id=1')
            self.assertEqual(restored.execute('SELECT policy_id FROM policy_source_reviews').fetchall(), [(2,)])
            self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.review_rows(), before)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_raw_notes_diagnostics_values_and_private_source_never_persist(self):
        review = {'code': 'structured_evidence_missing',
                  'note': 'PRIVATE_RAW_NOTE api_key=secretRawKey123456789',
                  'diagnostics': {'raw_reply': 'PRIVATE_RAW_DIAGNOSTIC'},
                  'fields': ['contact', 'start_age_months', {'contact': 'PRIVATE_FIELD_VALUE'}],
                  'contact': 'PRIVATE_CONTACT_VALUE', 'start_age_months': 24,
                  'raw': 'PRIVATE_RAW_MODEL_RESPONSE'}
        before = copy.deepcopy(review)
        self.assertTrue(store.record_hold(1, '담당자 private@example.com',
            ['https://example.invalid/policy?token=privateCredential123456789'],
            'PRIVATE_SOURCE_BODY', review))
        item = store.list_unresolved(['medical'])['items'][0]
        self.assertEqual(item['title'], '정책 #1')
        self.assertEqual(item['source_url'], '')
        self.assertEqual(item['fields'], ['contact', 'start_age_months'])
        self.assertEqual(item['source_hash'], hashlib.sha256(b'PRIVATE_SOURCE_BODY').hexdigest())
        persisted_and_visible = json.dumps([self.review_rows(), item], ensure_ascii=False)
        for secret in ('private@example.com', 'privateCredential', 'PRIVATE_SOURCE_BODY',
                       'PRIVATE_RAW_NOTE', 'secretRawKey', 'PRIVATE_RAW_DIAGNOSTIC',
                       'PRIVATE_FIELD_VALUE', 'PRIVATE_CONTACT_VALUE', 'PRIVATE_RAW_MODEL_RESPONSE'):
            self.assertNotIn(secret, persisted_and_visible)
        self.assertEqual(review, before)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_tampered_valid_json_fields_are_rechecked_read_only(self):
        self.put('source_evidence_missing')
        cases = (["contact", "unknown", {"target_income": "private"}, False, "contact"],
                 {'contact': 'private'}, 'contact', 123, None,
                 ['unknown'] * 100 + ['target_income'])
        for fields in cases:
            with self.subTest(fields=repr(fields)[:100]):
                with database.get_connection() as conn:
                    conn.execute('UPDATE policy_source_reviews SET fields=?', (json.dumps(fields),))
                    conn.commit()
                raw_before = self.review_rows()
                item = store.list_unresolved(['medical'])['items'][0]
                self.assertEqual(item['fields'], ['contact'] if isinstance(fields, list) and fields[:1] == ['contact'] else [])
                self.assertEqual(self.review_rows(), raw_before)
        self.assertEqual(self.policy_rows(), POLICIES)

    def test_malformed_field_json_is_an_explicit_read_failure_not_an_empty_queue(self):
        self.put('source_range_invalid')
        with database.get_connection() as conn:
            conn.execute("UPDATE policy_source_reviews SET fields='{' ")
            conn.commit()
        before = self.review_rows()
        with self.assertRaises(json.JSONDecodeError):
            store.list_unresolved(['medical'])
        self.assertEqual(self.review_rows(), before)
        self.assertEqual(self.policy_rows(), POLICIES)


if __name__ == '__main__':
    unittest.main()

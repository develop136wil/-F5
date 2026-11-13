"""Known AI validation holds persist; only completed policy saves resolve them.

Disposable SQLite policy rows and mocked extraction/fetch/comparison only. These
tests exercise the caller/store boundary, not live model quality or Notion writes.
"""
import copy
import hashlib
import importlib
import json
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
import database
from services import policy_review_store as store, policy_sync as ps
from tests.test_policy_review_resume import URL, row_for


OLD = '교육비 월 3만원 지원.'
CURRENT = '교육비 월 5만원 지원.'
OTHER_URL = 'https://example.invalid/other-policy'
AI_CODES = (
    'source_field_incomplete', 'structured_evidence_missing',
    'source_schema_invalid', 'source_conditions_incomplete',
    'source_range_invalid', 'source_fact_changed',
    'source_evidence_mismatch', 'source_evidence_boundary_changed',
    'source_presentation_mismatch', 'source_evidence_missing',
)
CRITICAL = ('page_id', 'notion_fields', 'source_urls', 'source_hashes',
            'source_texts', 'last_extracted_fields', 'change_status',
            'pending_diff', 'audited_at')


def held(code='source_field_incomplete'):
    return {'review': {'code': code, 'note': '합성 검증 보류.',
                       'fields': ['contact', 'target_income', 'age_detail']},
            'fields': None}


def accepted(text=CURRENT):
    return {'fields': {'support_detail': text,
                       '_source_evidence': {'support_detail': text}}}


class PolicyAIReviewFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.close_all_connections(strict=True)
        self.temp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(
            database, 'DB_FILE', str(Path(self.temp.name) / 'ai-review.db')))
        for status in (ps.POLICY_STATUS, ps.AUDIT_STATUS, ps.ONE_CLICK_STATUS):
            self.stack.enter_context(patch.dict(status, copy.deepcopy(status), clear=True))
        ps.ONE_CLICK_STATUS['running'] = False
        with database.get_connection() as conn:
            conn.execute('''CREATE TABLE notion_policies (
                id INTEGER PRIMARY KEY, db_key TEXT, title TEXT, page_id TEXT,
                notion_fields TEXT, source_urls TEXT, source_hashes TEXT,
                source_texts TEXT, last_extracted_fields TEXT, change_status TEXT,
                pending_diff TEXT, dead_links TEXT, last_checked_at TEXT,
                last_changed_at TEXT, audited_at TEXT)''')
            conn.commit()
        self.insert_policy()

    def tearDown(self):
        database.close_all_connections(strict=True)
        self.stack.close()
        self.temp.cleanup()

    def insert_policy(self, policy_id=1, source=OLD, db_key='medical', url=URL):
        row = row_for(source)
        row.update(id=policy_id, db_key=db_key,
                   source_urls=json.dumps([url]),
                   source_hashes=json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
                   source_texts=json.dumps({url: source}))
        with database.get_connection() as conn:
            conn.execute('INSERT INTO notion_policies (%s) VALUES (%s)' % (
                ','.join(row), ','.join('?' for _ in row)), tuple(row.values()))
            conn.commit()

    def policy(self, policy_id=1):
        with database.get_connection() as conn:
            return dict(conn.execute('SELECT * FROM notion_policies WHERE id=?',
                                     (policy_id,)).fetchone())

    def critical(self, policy_id=1):
        row = self.policy(policy_id)
        # Existing checks reserialize only these source maps with
        # ensure_ascii=False. Compare their exact decoded content, while keeping
        # Notion content, pending proposals and AI baselines byte-for-byte strict.
        for key in ('source_texts', 'source_hashes'):
            row[key] = json.loads(row[key])
        return {key: row[key] for key in CRITICAL}

    def update_policy(self, **values):
        with database.get_connection() as conn:
            conn.execute('UPDATE notion_policies SET %s WHERE id=1' %
                         ','.join(key + '=?' for key in values), tuple(values.values()))
            conn.commit()

    def seed_hold(self, policy_id=1, source=CURRENT, url=URL, source_role='current'):
        self.assertTrue(store.record_hold(policy_id, '교육비', [url], source,
                                         held()['review'], source_role))

    async def invoke(self, mode='check', outcomes=(), source=CURRENT,
                     write_error=None, compare_error=None):
        remaining = iter(outcomes)

        async def extract(*args, review, **kwargs):
            try:
                outcome = next(remaining)
            except StopIteration as error:
                raise AssertionError('Unexpected extraction retry') from error
            if isinstance(outcome, BaseException):
                raise outcome
            review.update(copy.deepcopy(outcome.get('review', {})))
            return copy.deepcopy(outcome.get('fields'))

        write_mock = AsyncMock(wraps=ps._db_write_strict, side_effect=write_error)
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_write_strict', new=write_mock), \
             patch.object(ps, '_fetch_source', new=AsyncMock(
                 return_value=(source, source, source is None))) as fetch, \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, 'extract_policy_fields_async',
                          new=AsyncMock(side_effect=extract)) as ai, \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                 side_effect=AssertionError('No real SDK call is permitted'))) as sdk, \
             patch.object(ps, '_diff_fields_with_embeddings',
                          new=AsyncMock(return_value=[], side_effect=compare_error)) as compare:
            if mode == 'audit':
                result = await ps.run_initial_audit(['medical'])
            else:
                result = await ps.check_policies(['medical'], scraper=object())
        sdk.assert_not_awaited()
        if mode == 'audit':
            fetch.assert_not_awaited()
        return result, ai, write_mock, compare

    async def test_each_supported_ai_hold_is_persisted_without_touching_policy_content(self):
        for code in AI_CODES:
            with self.subTest(code=code):
                before = self.critical()
                result, ai, write, compare = await self.invoke(outcomes=[held(code)])
                ai.assert_awaited_once()
                compare.assert_not_awaited()
                write.assert_awaited_once()
                self.assertEqual(self.critical(), before)
                self.assertEqual(result['ai_failed'], 1)
                self.assertEqual(result['review_count'], 1)
                item = store.list_unresolved(['medical'])['items'][0]
                self.assertEqual(item['code'], code)
                self.assertEqual(item['source_role'], 'current')
                self.assertEqual(item['source_url'], URL)
                self.assertEqual(item['source_hash'], hashlib.sha256(CURRENT.encode()).hexdigest())
                self.assertEqual(set(item['fields']), {'contact', 'target_income', 'age_detail'})
                self.assertEqual(store.list_unresolved(['medical'])['total'], 1)

    async def test_each_supported_ai_hold_keeps_initial_audit_unfinished(self):
        for code in AI_CODES:
            with self.subTest(code=code):
                before = self.policy()
                result, ai, write, compare = await self.invoke('audit', [held(code)])
                ai.assert_awaited_once()
                write.assert_not_awaited()
                compare.assert_not_awaited()
                self.assertEqual(self.policy(), before)
                self.assertIsNone(self.policy()['audited_at'])
                self.assertEqual(result['review_count'], 1)
                item = store.list_unresolved(['medical'])['items'][0]
                self.assertEqual(item['code'], code)
                self.assertEqual(item['source_hash'], hashlib.sha256(OLD.encode()).hexdigest())

    async def test_restart_and_read_api_keep_ai_hold_without_analysis_or_mutation(self):
        import api_server
        await self.invoke(outcomes=[held()])
        before = store.list_unresolved(['medical'])
        policy_before = self.policy()
        database.close_all_connections(strict=True)
        importlib.reload(store)
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock()) as ai, \
             patch.object(store, 'resolve_after_success', wraps=store.resolve_after_success) as resolve:
            result = await api_server.policy_reviews_endpoint('medical', 50, 0)
        ai.assert_not_awaited()
        resolve.assert_not_called()
        self.assertEqual(result, before)
        self.assertEqual(self.policy(), policy_before)

    async def test_previous_source_hold_recovers_against_current_notion_snapshot(self):
        self.update_policy(last_extracted_fields=None)
        self.seed_hold()
        before = self.policy()['notion_fields']
        result, ai, write, compare = await self.invoke(outcomes=[accepted(), held('structured_evidence_missing')])
        ai.assert_awaited_once()
        compare.assert_awaited_once()
        self.assertEqual(self.policy()['notion_fields'], before)
        items = store.list_unresolved(['medical'])['items']
        self.assertEqual(items, [])
        self.assertEqual(result['ai_completed'], 1)
        self.assertEqual(result['ai_failed'], 0)

    async def test_unsupported_or_absent_review_never_replaces_existing_ai_hold(self):
        self.seed_hold()
        held_before = store.list_unresolved(['medical'])
        for mode in ('check', 'audit'):
            for code in ('model_abstained', 'ai_input_review_required', 'api_error',
                         'rate_limited', 'source_unavailable', None):
                with self.subTest(mode=mode, code=code):
                    before = self.critical()
                    outcome = held(code) if code else {'fields': None}
                    with patch.object(store, 'record_hold', wraps=store.record_hold) as record:
                        _, ai, _, compare = await self.invoke(mode, [outcome])
                    ai.assert_awaited_once()
                    record.assert_not_called()
                    compare.assert_not_awaited()
                    self.assertEqual(store.list_unresolved(['medical']), held_before)
                    self.assertEqual(self.critical(), before)

    async def test_supported_record_false_is_not_treated_as_success_or_resolution(self):
        self.seed_hold()
        for mode in ('check', 'audit'):
            with self.subTest(mode=mode):
                before = self.critical()
                held_before = store.list_unresolved(['medical'])
                with patch.object(store, 'record_hold', return_value=False) as record, \
                     patch.object(store, 'resolve_after_success', wraps=store.resolve_after_success) as resolve:
                    _, ai, _, _ = await self.invoke(mode, [held()])
                record.assert_called_once()
                ai.assert_awaited_once()
                resolve.assert_not_called()
                self.assertEqual(self.critical(), before)
                self.assertEqual(store.list_unresolved(['medical']), held_before)

    async def test_success_resolves_only_after_comparison_and_actual_database_save(self):
        self.insert_policy(2, db_key='education', url=OTHER_URL)
        self.seed_hold(2, url=OTHER_URL)
        for mode in ('check', 'audit'):
            with self.subTest(mode=mode):
                self.seed_hold()
                self.seed_hold(source=OLD, source_role='baseline')
                before = self.policy()
                observed = []
                original_resolve = store.resolve_after_success

                def resolve(*args):
                    observed.append(self.policy())
                    return original_resolve(*args)

                with patch.object(store, 'resolve_after_success', side_effect=resolve):
                    _, ai, write, compare = await self.invoke(mode, [accepted()])
                ai.assert_awaited_once()
                write.assert_awaited_once()
                compare.assert_awaited_once()
                self.assertEqual(len(observed), 1)
                self.assertEqual(json.loads(observed[0]['last_extracted_fields'])['support_detail'], CURRENT)
                for key in ('page_id', 'notion_fields'):
                    self.assertEqual(observed[0][key], before[key])
                if mode == 'audit':
                    self.assertIsNotNone(observed[0]['audited_at'])
                else:
                    self.assertEqual(json.loads(observed[0]['source_texts']), {URL: CURRENT})
                    self.assertIsNone(observed[0]['pending_diff'])
                    self.assertEqual(observed[0]['change_status'], 'none')
                self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
                self.assertEqual(store.list_unresolved(['education'])['total'], 1)

    async def test_success_cache_keeps_per_policy_save_and_resolution(self):
        self.insert_policy(2)
        self.seed_hold(1)
        self.seed_hold(2)
        _, ai, write, compare = await self.invoke(outcomes=[accepted()])
        ai.assert_awaited_once()
        self.assertEqual(write.await_count, 2)
        self.assertEqual(compare.await_count, 2)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
        for policy_id in (1, 2):
            row = self.policy(policy_id)
            self.assertEqual(json.loads(row['last_extracted_fields'])['support_detail'], CURRENT)
            self.assertEqual(json.loads(row['notion_fields']), {'support_detail': '사용자 내용'})

    async def test_compare_or_policy_write_failure_never_resolves_existing_ai_hold(self):
        self.seed_hold()
        for mode in ('check', 'audit'):
            for failure in ('compare', 'write'):
                with self.subTest(mode=mode, failure=failure):
                    before = self.policy()
                    held_before = store.list_unresolved(['medical'])
                    error = sqlite3.OperationalError('synthetic failure')
                    kwargs = {'compare_error': error} if failure == 'compare' else {'write_error': error}
                    with patch.object(store, 'resolve_after_success', wraps=store.resolve_after_success) as resolve:
                        with self.assertRaises(sqlite3.OperationalError):
                            await self.invoke(mode, [accepted()], **kwargs)
                    resolve.assert_not_called()
                    self.assertEqual(self.policy(), before)
                    self.assertEqual(store.list_unresolved(['medical']), held_before)

    async def test_hold_record_failure_keeps_reason_and_propagates_sanitized_error(self):
        self.seed_hold()
        for mode, status in (('check', ps.POLICY_STATUS), ('audit', ps.AUDIT_STATUS)):
            with self.subTest(mode=mode):
                before = self.policy()
                held_before = store.list_unresolved(['medical'])
                with patch.object(store, 'record_hold', side_effect=sqlite3.OperationalError('private path')), \
                     patch.object(store, 'resolve_after_success', wraps=store.resolve_after_success) as resolve:
                    with self.assertRaisesRegex(RuntimeError, '보류 기록을 저장하지 못해') as raised:
                        await self.invoke(mode, [held('source_schema_invalid')])
                resolve.assert_not_called()
                self.assertNotIn('private', str(raised.exception))
                self.assertFalse(status['running'])
                self.assertEqual(status['review_count'], 1)
                self.assertEqual(status['review_notes'][0]['code'], 'source_schema_invalid')
                self.assertIn('보류 기록을 저장하지 못해', status['message'])
                self.assertNotIn('점검 완료', status['message'])
                self.assertNotIn('감사 완료', status['message'])
                self.assertEqual(self.policy(), before)
                self.assertEqual(store.list_unresolved(['medical']), held_before)

    async def test_transport_or_fetch_failure_cannot_resolve_existing_ai_hold(self):
        self.seed_hold()
        before = self.critical()
        held_before = store.list_unresolved(['medical'])
        _, ai, _, _ = await self.invoke(source=None)
        ai.assert_not_awaited()
        after_fetch = store.list_unresolved(['medical'])
        self.assertEqual([item for item in after_fetch['items'] if item['source_role'] == 'current'], held_before['items'])
        self.assertTrue(any(item['source_role'] == 'fetch' for item in after_fetch['items']))
        with self.assertRaisesRegex(RuntimeError, 'synthetic transport'):
            await self.invoke('check', [RuntimeError('synthetic transport')])
        self.assertEqual(self.critical(), before)
        _, ai, _, _ = await self.invoke('audit', [RuntimeError('must not read stale cached body')])
        ai.assert_not_awaited()
        self.assertEqual(store.list_unresolved(['medical']), after_fetch)

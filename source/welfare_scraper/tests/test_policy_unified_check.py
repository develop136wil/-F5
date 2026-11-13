"""Unified policy checks with synthetic SQLite; AI, source and Notion are mocked."""
import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import database
from services import policy_full_check, policy_sync as ps
from tests import test_check_policies_baseline_failure as fixtures


OLD = {'support_detail': '월 10만원 지급'}
NEW = {'support_detail': '월 20만원 지급'}
SOURCE = '지원내용: 월 20만원 지급. 신청 자격: 등록장애인.'


class UnifiedCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        database.execute_write(
            'UPDATE notion_policies SET notion_fields=?,source_hashes=?,source_texts=?',
            (json.dumps(OLD), '{}', '{}'))
        self.writes = AsyncMock(side_effect=AssertionError('Notion writes are forbidden'))
        self.creates = AsyncMock(side_effect=AssertionError('Notion creates are forbidden'))
        for patcher in (
            patch.object(ps.notion_service, 'update_page_properties', self.writes),
            patch.object(ps.notion_service, 'create_policy_page', self.creates),
            patch.object(ps, '_get_db_schema', return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.writes.assert_not_called()
        self.creates.assert_not_called()
        fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown(self)

    def row(self, page_id=fixtures._PAGE_ID):
        return dict(database.execute_query('SELECT * FROM notion_policies WHERE page_id=?',
                                          (page_id,), fetch_one=True))

    def seed_previous_source(self):
        database.execute_write('UPDATE notion_policies SET source_hashes=?,source_texts=?',
            (json.dumps({fixtures._URL: 'previous-hash'}),
             json.dumps({fixtures._URL: '지원내용: 월 5만원 지급'})))

    def add_policy(self, page_id):
        url = 'https://test.example/' + page_id
        database.execute_write(
            'INSERT INTO notion_policies(db_key,page_id,title,notion_fields,source_urls,source_hashes,source_texts) '
            'VALUES(?,?,?,?,?,?,?)',
            ('medical', page_id, '테스트 정책 ' + page_id, json.dumps(OLD), json.dumps([url]), '{}', '{}'))

    async def run_check(self, *, ai=None, reason='', client=True, source=None):
        ai = ai if ai is not None else AsyncMock(return_value=copy.deepcopy(NEW))

        async def compare(before, after, _client):
            return ps._diff_fields(before, after, None, {})

        compare_mock = AsyncMock(side_effect=compare)
        reason_mock = patch.object(policy_full_check, 'ai_pause_reason',
            **({'side_effect': reason} if callable(reason) else {'return_value': reason}))
        fetched = source if source is not None else (SOURCE, SOURCE, False)
        with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=fetched)), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object() if client else None), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=ai), \
             patch.object(ps, '_diff_fields_with_embeddings', new=compare_mock), reason_mock:
            result = await ps.check_policies(['medical'], scraper=object(),
                                            include_initial=True, stop_on_quota=True)
        return result, ai, compare_mock

    async def test_first_source_read_compares_actual_notion_once_and_saves_proposal(self):
        result, ai, compared = await self.run_check()
        self.assertEqual(result['initial_compared'], 1)
        self.assertEqual(result['changed'], 1)
        ai.assert_awaited_once()
        compared.assert_awaited_once()
        self.assertEqual(compared.await_args.args[0]['support_detail'], OLD['support_detail'])
        row = self.row()
        self.assertIsNotNone(row['audited_at'])
        self.assertEqual(json.loads(row['notion_fields']), OLD)
        pending = json.loads(row['pending_diff'])
        field = next(f for f in pending['fields'] if f['field'] == ps._PROP_LABEL['support_detail'])
        self.assertEqual(field['old'], OLD['support_detail'])
        self.assertEqual(field['new'], NEW['support_detail'])
        self.assertTrue(field['snapshot_current'])

    async def test_second_unchanged_check_spends_no_ai_and_preserves_completed_comparison(self):
        await self.run_check()
        before = self.row()
        result, ai, compared = await self.run_check()
        ai.assert_not_awaited()
        compared.assert_not_awaited()
        self.assertEqual(result['initial_compared'], 0)
        self.assertEqual(result['ai_required'], 0)
        after = self.row()
        for key in ('audited_at', 'last_extracted_fields', 'pending_diff', 'notion_fields'):
            self.assertEqual(after[key], before[key], key)

    async def test_changed_source_without_audit_still_uses_one_ai_and_actual_notion(self):
        self.seed_previous_source()
        database.execute_write('UPDATE notion_policies SET last_extracted_fields=?',
                               (json.dumps({'support_detail': '월 5만원 지급'}),))
        result, ai, compared = await self.run_check()
        ai.assert_awaited_once()
        self.assertEqual(compared.await_args.args[0]['support_detail'], OLD['support_detail'])
        self.assertEqual(result['initial_compared'], 1)
        self.assertIsNotNone(self.row()['audited_at'])

    async def test_missing_ai_does_not_stamp_audit_or_replace_previous_source(self):
        self.seed_previous_source()
        before = self.row()
        result, ai, compared = await self.run_check(client=False, reason='ai_connection_required')
        ai.assert_not_awaited()
        compared.assert_not_awaited()
        self.assertEqual(result['pause_reason'], 'ai_connection_required')
        row = self.row()
        self.assertIsNone(row['audited_at'])
        for key in ('source_hashes', 'source_texts', 'last_extracted_fields', 'pending_diff', 'notion_fields'):
            self.assertEqual(json.loads(row[key]) if row[key] is not None else None,
                             json.loads(before[key]) if before[key] is not None else None, key)

    async def test_existing_daily_hold_stops_before_ai_without_advancing_baseline(self):
        self.seed_previous_source()
        before = self.row()
        result, ai, _ = await self.run_check(reason='daily_limit')
        ai.assert_not_awaited()
        self.assertEqual(result['pause_reason'], 'daily_limit')
        row = self.row()
        self.assertIsNone(row['audited_at'])
        self.assertEqual(row['source_hashes'], before['source_hashes'])
        self.assertEqual(json.loads(row['source_texts']), json.loads(before['source_texts']))
        self.assertIsNone(row['last_extracted_fields'])

    async def test_incomplete_current_source_keeps_initial_audit_retryable(self):
        self.seed_previous_source()
        before = self.row()
        source = ps.SourceFetchResult(SOURCE, SOURCE, False, source_review={
            'code': 'source_read_incomplete', 'fields': [], 'note': 'Synthetic partial source'})
        result, ai, compared = await self.run_check(source=source)
        ai.assert_not_awaited()
        compared.assert_not_awaited()
        self.assertGreater(result['review_count'], 0)
        row = self.row()
        self.assertIsNone(row['audited_at'])
        self.assertEqual(row['source_hashes'], before['source_hashes'])
        self.assertIsNone(row['last_extracted_fields'])

    async def test_failed_first_extraction_retries_without_audit_stamp(self):
        result, ai, _ = await self.run_check(ai=AsyncMock(return_value=None))
        ai.assert_awaited_once()
        self.assertEqual(result['ai_failed'], 1)
        self.assertIsNone(self.row()['audited_at'])
        self.assertIsNone(self.row()['pending_diff'])
        recovered, retried, _ = await self.run_check()
        retried.assert_awaited_once()
        self.assertEqual(recovered['initial_compared'], 1)
        self.assertIsNotNone(self.row()['audited_at'])

    async def test_pause_mid_batch_preserves_finished_and_leaves_rest_for_next_click(self):
        self.add_policy('second')
        self.add_policy('third')
        ai = AsyncMock(return_value=copy.deepcopy(NEW))
        result, _, _ = await self.run_check(ai=ai,
            reason=lambda **_kwargs: 'local_budget_exhausted' if ai.await_count >= 1 else '')
        self.assertEqual(result['pause_reason'], 'local_budget_exhausted')
        self.assertEqual(result['checked'], 2, 'The unvisited tail is not counted as checked')
        ai.assert_awaited_once()
        completed = self.row()
        self.assertIsNotNone(completed['audited_at'])
        self.assertIsNone(self.row('second')['audited_at'])
        self.assertIsNone(self.row('third')['audited_at'])
        self.assertIsNone(self.row('third')['last_checked_at'], 'Do not sweep the unvisited tail')
        recovered, retried, _ = await self.run_check()
        self.assertEqual(retried.await_count, 2)
        self.assertEqual(recovered['initial_compared'], 2)
        self.assertEqual(self.row()['pending_diff'], completed['pending_diff'])
        self.assertEqual(self.row()['audited_at'], completed['audited_at'])
        self.assertIsNotNone(self.row('second')['audited_at'])
        self.assertIsNotNone(self.row('third')['audited_at'])

    async def test_quota_swallowed_by_classifier_stops_after_failed_item(self):
        self.add_policy('second')
        self.add_policy('third')
        ai = AsyncMock(side_effect=[copy.deepcopy(NEW), None])
        result, _, _ = await self.run_check(ai=ai,
            reason=lambda **_kwargs: 'daily_limit' if ai.await_count >= 2 else '')
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(ai.await_count, 2)
        self.assertIsNotNone(self.row()['audited_at'])
        self.assertIsNone(self.row('second')['audited_at'])
        self.assertIsNone(self.row('second')['last_extracted_fields'])
        self.assertIsNone(self.row('third')['last_checked_at'])

    async def test_applied_receipt_survives_new_initial_proposal(self):
        receipt = {'field': ps._PROP_LABEL['eligibility_detail'], 'old': '이전 자격',
                   'new': '등록장애인', 'level': 'high', 'applied': True,
                   'undo_id': 'synthetic-applied-receipt',
                   'acceptance': {'key': 'eligibility_detail', 'written': '등록장애인',
                                  'extracted': '등록장애인'}}
        database.execute_write('UPDATE notion_policies SET pending_diff=?,change_status=?',
                               (json.dumps({'fields': [receipt]}), 'detected'))
        result, ai, _ = await self.run_check()
        ai.assert_awaited_once()
        self.assertEqual(result['changed'], 1)
        fields = json.loads(self.row()['pending_diff'])['fields']
        self.assertIn(receipt, fields)
        self.assertTrue(any(not field.get('applied') for field in fields))

    async def test_matching_first_comparison_marks_audited_without_a_proposal(self):
        result, ai, _ = await self.run_check(ai=AsyncMock(return_value=copy.deepcopy(OLD)))
        ai.assert_awaited_once()
        self.assertEqual(result['initial_compared'], 1)
        self.assertEqual(result['changed'], 0)
        self.assertIsNotNone(self.row()['audited_at'])
        self.assertIsNone(self.row()['pending_diff'])

    def seed_completed_acceptance(self, *, missing=False, uncertain=False, current=None):
        raw, written = '보호자 동승 시 50% 할인', '보호자 동승 시 50% 감면'
        receipt = {'field': ps._PROP_LABEL['eligibility_detail'], 'old': '이전 조건',
                   'new': written, 'level': 'high', 'applied': True,
                   'undo_id': 'synthetic-completed-receipt'}
        if not missing:
            receipt['acceptance'] = {'key': 'eligibility_detail', 'written': written, 'extracted': raw}
        if uncertain:
            receipt['needs_verification'] = True
        database.execute_write(
            'UPDATE notion_policies SET notion_fields=?,last_extracted_fields=?,pending_diff=?,change_status=?',
            (json.dumps({'eligibility_detail': current if current is not None else written}),
             json.dumps({'eligibility_detail': raw}),
             json.dumps({'fields': [receipt], 'completed': True}), 'detected'))
        return raw, receipt

    async def check_accepted(self, raw):
        source = '신청 자격: ' + raw
        return await self.run_check(ai=AsyncMock(return_value={'eligibility_detail': raw}),
                                    source=(source, source, False))

    async def test_initial_comparison_keeps_exact_accepted_styled_pair_completed(self):
        raw, receipt = self.seed_completed_acceptance()
        result, ai, _ = await self.check_accepted(raw)
        ai.assert_awaited_once()
        self.assertEqual(result['changed'], 0)
        self.assertEqual(result['initial_compared'], 1)
        pending = json.loads(self.row()['pending_diff'])
        self.assertTrue(pending['completed'])
        self.assertEqual(pending['fields'], [receipt])
        again, retried, _ = await self.check_accepted(raw)
        retried.assert_not_awaited()
        self.assertEqual(again['changed'], 0)

    async def test_changed_fact_reopens_accepted_pair_and_keeps_receipt_history(self):
        _, receipt = self.seed_completed_acceptance()
        result, _, _ = await self.check_accepted('보호자 동승 시 30% 할인')
        self.assertEqual(result['changed'], 1)
        fields = json.loads(self.row()['pending_diff'])['fields']
        self.assertIn(receipt, fields)
        self.assertTrue(any(not field.get('applied') and '30%' in field['new'] for field in fields))

    async def test_manual_notion_edit_is_not_hidden_by_old_acceptance(self):
        raw, receipt = self.seed_completed_acceptance(current='보호자 동승 시 30% 감면')
        result, _, _ = await self.check_accepted(raw)
        self.assertEqual(result['changed'], 1)
        fields = json.loads(self.row()['pending_diff'])['fields']
        self.assertIn(receipt, fields)
        self.assertTrue(any(not field.get('applied') and '30%' in field['old'] for field in fields))

    async def test_legacy_applied_marker_without_acceptance_does_not_suppress_review(self):
        raw, receipt = self.seed_completed_acceptance(missing=True)
        result, _, _ = await self.check_accepted(raw)
        self.assertEqual(result['changed'], 1)
        self.assertIn(receipt, json.loads(self.row()['pending_diff'])['fields'])

    async def test_uncertain_receipt_does_not_suppress_initial_comparison(self):
        raw, receipt = self.seed_completed_acceptance(uncertain=True)
        result, _, _ = await self.check_accepted(raw)
        self.assertEqual(result['changed'], 1)
        self.assertIn(receipt, json.loads(self.row()['pending_diff'])['fields'])

    async def test_standalone_initial_audit_without_client_does_not_stamp(self):
        normalized = ps.policy_watch._normalize_text(SOURCE)
        database.execute_write('UPDATE notion_policies SET source_hashes=?,source_texts=?',
            (json.dumps({fixtures._URL: hashlib.sha256(normalized.encode()).hexdigest()}),
             json.dumps({fixtures._URL: SOURCE})))
        with patch.object(ps.ai_classifier, 'get_client', return_value=None), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock()) as ai:
            result = await ps.run_initial_audit(['medical'])
        ai.assert_not_awaited()
        self.assertEqual(result['changed'], 0)
        self.assertIsNone(self.row()['audited_at'])
        self.assertIsNone(self.row()['last_extracted_fields'])


if __name__ == '__main__':
    unittest.main()

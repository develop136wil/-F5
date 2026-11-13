"""Connected policy completion regressions with a temporary SQLite mirror.

Real: full-check orchestration, check_policies with an inert scraper, comparison,
persistence, fill_missing_embeddings, change cards and persisted review reads.
Mocked stages: successful sync against a seeded mirror, no-op recomputation,
unconfigured style. Source retrieval, AI extraction/embedding results, schema,
client presence and quota hints are mocked; all HTTP/Notion writes are blocked.
This does not exercise remote creation/sync, classifier internals or live quota.
"""
import copy
import hashlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import database
from services import policy_full_check as flow, policy_review_store, policy_sync as ps
from tests import test_check_policies_baseline_failure as fixtures

FIELDS = {'category': '의료·재활', 'support_detail': '월 10만원 지급'}
SOURCE = '테스트 정책. 지원내용: 월 10만원 지급. 신청 자격: 등록장애인.'
KEYS = ['medical']


class PolicyStatusJourney(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        database.execute_write(
            'UPDATE notion_policies SET notion_fields=?, source_hashes=?, '
            'source_texts=?, last_extracted_fields=NULL, audited_at=NULL, '
            'pending_diff=NULL, change_status=?, title_embedding=NULL',
            (json.dumps(FIELDS, ensure_ascii=False), '{}', '{}', 'none'))

    def tearDown(self):
        fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown(self)

    def row(self):
        return dict(database.execute_query('SELECT * FROM notion_policies WHERE page_id=?',
            (fixtures._PAGE_ID,), fetch_one=True))

    async def run_journey(self, *, extraction, embedding, expected_extractions=1, expected_embeddings=1):
        real_check = ps.check_policies
        checked = []

        async def actual_check(db_keys, **kwargs):
            result = await real_check(db_keys, scraper=object(), **kwargs)
            checked.append(copy.deepcopy(result))
            return result

        async def vectors(texts, **_kwargs):
            return [copy.deepcopy(embedding) for _text in texts]

        with ExitStack() as stack:
            for status in (ps.ONE_CLICK_STATUS, ps.SYNC_STATUS, ps.POLICY_STATUS,
                           ps.AUDIT_STATUS, ps.RECOMPUTE_STATUS, ps.STYLE_STATUS):
                stack.enter_context(patch.dict(status, {}, clear=True))
            sync = stack.enter_context(patch.object(ps, 'sync_policies',
                new=AsyncMock(return_value={'ok': True, 'synced': 1})))
            stack.enter_context(patch.object(ps, 'recompute_diffs',
                new=AsyncMock(return_value={'ok': True, 'checked': 0})))
            check = stack.enter_context(patch.object(ps, 'check_policies',
                new=AsyncMock(side_effect=actual_check)))
            stack.enter_context(patch.object(ps, 'preview_style_work',
                return_value={'configured': False, 'eligible_count': 0}))
            style = stack.enter_context(patch.object(ps, 'restyle_pending_diffs',
                new=AsyncMock(side_effect=AssertionError('Unexpected style stage'))))
            stack.enter_context(patch.object(ps, '_get_db_schema', return_value={}))
            fetch = stack.enter_context(patch.object(ps, '_fetch_source',
                new=AsyncMock(return_value=(SOURCE, SOURCE, False))))
            stack.enter_context(patch.object(ps.ai_classifier, 'get_client', return_value=object()))
            extract = stack.enter_context(patch.object(ps.ai_classifier, 'extract_policy_fields_async',
                new=AsyncMock(return_value=copy.deepcopy(extraction))))
            stack.enter_context(patch.object(flow, 'ai_pause_reason', return_value=''))
            stack.enter_context(patch('services.ai_coordinator.input_request_budget', return_value=1))
            stack.enter_context(patch.object(ps.embedding_service, 'get_unavailable_reason', return_value=None))
            embed = stack.enter_context(patch.object(ps.embedding_service, 'embed_texts_batch_async',
                new=AsyncMock(side_effect=vectors)))
            forbidden = [stack.enter_context(patch.object(ps.notion_service, name,
                new=AsyncMock(side_effect=AssertionError('Unexpected Notion write'))))
                for name in ('create_policy_page', 'update_page_properties')]
            http = stack.enter_context(patch('aiohttp.ClientSession._request',
                new=AsyncMock(side_effect=AssertionError('Unexpected HTTP request'))))

            # Neither run_full_check nor fill_missing_embeddings is replaced.
            result = await ps.run_full_check(KEYS)
            status = copy.deepcopy(ps.ONE_CLICK_STATUS)
            changes = ps.get_changes(KEYS)
            reviews = policy_review_store.unresolved_by_policy(KEYS)
            sync.assert_awaited_once_with(KEYS, defer_embeddings=True)
            check.assert_awaited_once_with(KEYS, include_initial=True, stop_on_quota=True)
            fetch.assert_awaited_once()
            self.assertEqual(extract.await_count, expected_extractions)
            # Matching fields need no comparison embeddings. This is title backfill.
            self.assertEqual(embed.await_count, expected_embeddings)
            if expected_embeddings:
                embed.assert_awaited_once_with(['테스트 정책'])
            style.assert_not_awaited()
            http.assert_not_called()
            for call in forbidden:
                call.assert_not_called()
        return result, status, changes, reviews, checked[0]

    def assert_no_item_partial(self, result, status, changes, reviews):
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['pause_reason'], '')
        self.assertEqual(status['review_count'], 0)
        self.assertEqual(status['review_notes'], [])
        self.assertEqual(changes, [])
        self.assertEqual(reviews, {})
        self.assertNotIn('검토 안내가 있는 항목', status['message'])
        self.assertNotIn('확인이 필요한 정책', status['message'])
        self.assertIn('정책 정보 최신인지 확인하기', status['message'])
        self.assertIn('재시도', status['message'])
        self.assertIn('완료한 비교와 제안은 보존', status['message'])
        self.assertFalse(status['running'])
        self.assertEqual(status['stages'], ['sync', 'recompute', 'check', 'embedding'])

    async def test_successful_first_comparison_then_embedding_none_has_no_review_item(self):
        result, status, changes, reviews, checked = await self.run_journey(
            extraction=FIELDS, embedding=None)
        self.assert_no_item_partial(result, status, changes, reviews)
        self.assertEqual(checked['initial_compared'], 1)
        self.assertEqual(checked['changed'], 0)
        self.assertEqual(checked['ai_completed'], 1)
        self.assertEqual(checked['ai_failed'], 0)
        self.assertEqual(status['initial_compared'], 1)
        self.assertEqual(status['changed'], 0)
        self.assertEqual(status['ai_failed'], 0)
        row = self.row()
        self.assertIsNotNone(row['audited_at'], 'The policy comparison really completed')
        self.assertIsNone(row['title_embedding'], 'Auxiliary failure stays retryable')
        self.assertIsNone(row['pending_diff'])
        self.assertEqual(json.loads(row['notion_fields']), FIELDS)
        self.assertEqual(status['embedding_failed'], 1)
        self.assertEqual(status['embedding_completed'], 0)
        self.assertEqual(status['style_failed'], 0)
        self.assertEqual(status['style_rejected_fields'], 0)
        self.assertEqual(result['embedding_failed'], 1)
        self.assertIn('제목 검색 데이터 준비 실패 1건', status['message'])
        self.assertNotIn('AI 분석 미완료', status['message'])
        recovered, resumed, _, _, retried = await self.run_journey(
            extraction=FIELDS, embedding=[1.0, 0.0], expected_extractions=0)
        self.assertEqual(recovered['state'], 'completed')
        self.assertEqual(resumed['embedding_failed'], 0)
        self.assertEqual(resumed['embedding_completed'], 1)
        self.assertEqual(retried['initial_compared'], 0)
        self.assertEqual(self.row()['audited_at'], row['audited_at'])
        self.assertEqual(self.row()['last_extracted_fields'], row['last_extracted_fields'])
        self.assertIsNotNone(self.row()['title_embedding'])

    async def test_generic_extraction_none_has_failed_count_but_no_review_item(self):
        result, status, changes, reviews, checked = await self.run_journey(
            extraction=None, embedding=[1.0, 0.0])
        self.assert_no_item_partial(result, status, changes, reviews)
        self.assertEqual(checked['initial_compared'], 0)
        self.assertEqual(checked['ai_required'], 1)
        self.assertEqual(checked['ai_completed'], 0)
        self.assertEqual(checked['ai_failed'], 1)
        self.assertEqual(status['ai_failed'], 1)
        row = self.row()
        self.assertIsNone(row['audited_at'], 'A failed extraction cannot finish first comparison')
        self.assertIsNone(row['last_extracted_fields'])
        self.assertIsNone(row['pending_diff'])
        self.assertIsNotNone(row['title_embedding'], 'No auxiliary failure confounds this case')
        self.assertEqual(json.loads(row['notion_fields']), FIELDS)
        # A first fetch establishes the source hash/text even when extraction
        # fails: there is no previous hash to restore. This is NOT a completed
        # AI baseline. audited_at and last_extracted_fields remain NULL above,
        # so include_initial=True retries even if this source hash is unchanged.
        self.assertEqual(json.loads(row['source_hashes']),
                         {fixtures._URL: hashlib.sha256(SOURCE.encode('utf-8')).hexdigest()})
        self.assertEqual(json.loads(row['source_texts']), {fixtures._URL: SOURCE})
        self.assertEqual(status['embedding_failed'], 0)
        self.assertEqual(status['embedding_completed'], 1)
        self.assertEqual(status['style_failed'], 0)
        self.assertEqual(result['embedding_completed'], 1)
        self.assertIn('AI 분석 미완료 1건', status['message'])
        self.assertNotIn('제목 검색 데이터 준비 실패', status['message'])
        recovered, resumed, _, _, retried = await self.run_journey(
            extraction=FIELDS, embedding=[1.0, 0.0], expected_embeddings=0)
        self.assertEqual(recovered['state'], 'completed')
        self.assertEqual(resumed['ai_failed'], 0)
        self.assertEqual(retried['initial_compared'], 1)
        self.assertIsNotNone(self.row()['audited_at'])
        self.assertIsNotNone(self.row()['last_extracted_fields'])
        self.assertEqual(json.loads(self.row()['notion_fields']), FIELDS)


if __name__ == '__main__':
    unittest.main()

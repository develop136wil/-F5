"""Source-only holds must remain visible without rate waiting or data loss."""
import hashlib
import json
import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import ai_classifier
from services import policy_sync as ps

SOURCE = '지원 내용: 교육비 지원\n연령: 18~39세 (출생연도: 1986~2008년)'
URL = 'https://example.invalid/policy'

def row_for(source='기존 원문'):
    return dict(id=1, page_id='fake', title='교육비', db_key='medical',
        notion_fields=json.dumps({'support_detail': '사용자 내용'}),
        source_urls=json.dumps([URL]), source_hashes=json.dumps({URL: hashlib.sha256(source.encode()).hexdigest()}),
        source_texts=json.dumps({URL: source}), last_extracted_fields=json.dumps({'support_detail': '기존 기준'}),
        change_status='detected', pending_diff=json.dumps({'fields': [{'field': '상세 지원 내용', 'new': '기존 제안'}],
            'extraction_meta': {'prompt_version': ps._EXTRACTION_PROMPT_VERSION}}))

class ReviewResume(unittest.IsolatedAsyncioTestCase):
    async def check(self, row, source, response=None):
        clock_guard = patch.object(ps, 'time', SimpleNamespace(
            monotonic=Mock(side_effect=AssertionError('PreflightMustNotReserveRateSlot')))) if response is None else nullcontext()
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=response)) as call, \
             patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])), \
             clock_guard:
            result = await ps.check_policies(['medical'], scraper=object())
        return result, write, call

    async def test_repeated_held_source_preserves_baseline_and_proposal_without_rate_slot(self):
        row = row_for()
        for _ in range(2):
            result, write, call = await self.check(row, SOURCE)
            call.assert_not_awaited()
            self.assertEqual(result['ai_preflight_held'], 1)
            self.assertEqual(result['ai_required'], 0)
            self.assertEqual(result['ai_failed'], 0)
            self.assertEqual(result['ai_rate_waited'], 0)
            self.assertEqual(result['review_count'], 1)
            sql, values = write.await_args.args
            for column in ('notion_fields=', 'pending_diff=', 'last_extracted_fields='):
                self.assertNotIn(column, sql)
            self.assertEqual(json.loads(values[0]), json.loads(row['source_hashes']))
            self.assertEqual(json.loads(values[1]), json.loads(row['source_texts']))

    async def test_unchanged_current_proposal_redisplays_hold_without_reanalysis(self):
        result, write, call = await self.check(row_for(SOURCE), SOURCE)
        call.assert_not_awaited()
        self.assertEqual(result['review_count'], 1)
        self.assertEqual(result['ai_preflight_held'], 1)
        self.assertNotIn('pending_diff=', write.await_args.args[0])

    async def test_clear_source_resumes_normal_analysis_after_hold(self):
        row = row_for()
        await self.check(row, SOURCE)
        clear = '교육비 월 5만원 지원.'
        response = {'support_detail': clear, '_source_evidence': {'support_detail': clear}}
        result, write, call = await self.check(row, clear, response)
        call.assert_awaited_once()
        self.assertEqual(result['ai_preflight_held'], 0)
        self.assertEqual(result['review_count'], 0)
        self.assertEqual(result['ai_completed'], 1)
        self.assertIn('last_extracted_fields=', write.await_args.args[0])
        self.assertNotIn('notion_fields=', write.await_args.args[0])

    async def test_full_check_propagates_new_counter(self):
        with patch.object(ps, 'sync_policies', new=AsyncMock(return_value={'ok': True})), \
             patch.object(ps, 'check_policies', new=AsyncMock(return_value={
                 'ok': True, 'ai_preflight_held': 2, 'review_count': 2, 'review_notes': []})):
            result = await ps.run_full_check(['medical'])
        self.assertEqual(result['ai_preflight_held'], 2)
        self.assertEqual(ps.ONE_CLICK_STATUS['ai_preflight_held'], 2)

class ReviewPreflight(unittest.TestCase):
    def test_length_precedes_criteria_and_notes_are_fresh(self):
        with patch.dict(os.environ, {'POLICY_SOURCE_MAX_CHARS': '5'}):
            self.assertEqual(ai_classifier.policy_source_preflight_review(SOURCE)['code'], 'source_too_long')
        first = ai_classifier.policy_source_preflight_review(SOURCE)
        first['fields'].clear()
        self.assertEqual(ai_classifier.policy_source_preflight_review(SOURCE)['fields'], ['age_detail'])
        self.assertEqual(ai_classifier.policy_source_preflight_review('교육비 지원'), {})

    def test_summary_names_source_criteria_instead_of_generic_failure(self):
        summary = ps._extraction_review_summary({'review_count': 1,
            'review_notes': [{'code': 'source_criteria_review_required'}]})
        self.assertIn('소득·나이 적용 기준 확인 필요', summary)
        self.assertNotIn('원문 근거 확인 필요', summary)

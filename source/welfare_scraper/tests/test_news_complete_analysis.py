"""Synthetic complete-source regression; no network or operational user data."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
from services import ai_verified_input as verified, ai_coordinator as quota
from services import news_custom_analysis as custom, news_analysis_store as jobs
from services import news_unified_feed as feed, news_default_condition as defaults
from services import news_source_selection as selection, page_coverage
from services.news_complete_analysis import _checkpoint, _identity, _merge, _split
from services.news_analysis_worker import process_job
from tests import test_news_custom_input_budget as fixtures


class CompleteNewsInput(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CurrentCustomInputBudget.setUp
    call_db = fixtures.CurrentCustomInputBudget.call_db
    response = fixtures.CurrentCustomInputBudget.response
    output = fixtures.CurrentCustomInputBudget.output

    def enqueue(self, source=fixtures.SHORT, **kwargs):
        return fixtures.CurrentCustomInputBudget.enqueue(self, source, recipe=custom.RECIPE, **kwargs)

    def setup_provider(self, *, count=1800, reply=None):
        verified._reset_for_tests()
        self.addCleanup(verified._reset_for_tests)
        # Test only: remove auxiliary pacing and generation waiting; quota
        # admission is separately exercised by the single-call integration test.
        token_mock = AsyncMock(return_value=SimpleNamespace(total_tokens=count))
        self.client.aio.models.count_tokens = token_mock
        pacing = patch.object(verified, '_take_count_slot', new=AsyncMock(return_value=True))
        pacing.start()
        self.addCleanup(pacing.stop)
        raw = self.response() if reply is None else reply
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw, ensure_ascii=False))]), finish_reason='STOP')])
        return token_mock

    def source(self, length):
        body = fixtures.SHORT + '\n' + ('사업 안내를 충분히 읽어주세요.\n' * (length // 16 + 1))
        return body[:length] + '\n마지막 제외 조건: 동일한 사업의 지원금은 중복 신청할 수 없습니다.'

    async def run_job(self, job):
        return await process_job(job, self.client, call_db=self.call_db, clock=lambda: 0)

    async def test_3000_korean_chars_keep_tail_and_use_real_coordinator_once(self):
        counter = self.setup_provider()
        source = self.source(3000)
        job = self.enqueue(source)
        self.assertEqual(await self.run_job(job), 'complete', jobs.get_job(self.conn, job))
        self.provider.assert_awaited_once()
        counter.assert_awaited_once()
        counted = json.loads(counter.await_args.kwargs['contents'].split('\nSOURCE: ')[1])
        self.assertEqual(counted['content'], source)
        result = self.output(job)
        self.assertTrue(result['accepted'], result)
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(result['analysis']['source_input']['status'], 'complete')
        self.assertEqual(quota.status()['recorded_requests_today'], 1)
        self.assertEqual(quota.input_token_budget(self.model), 6000)
        self.assertEqual(feed.read_feed(self.conn, defaults.PROFILE_ID, view='ready')['total'], 1)
        self.assertIsNone(jobs.get_job(self.conn, job)['checkpoint_json'])

    async def test_6000_and_15000_plus_are_not_character_truncated(self):
        self.setup_provider()
        with patch.object(quota, 'acquire_model_slot', new_callable=AsyncMock) as admit:
            for length in (6000, 15500):
                source = self.source(length)
                job = self.enqueue(source)
                self.assertEqual(await self.run_job(job), 'complete', jobs.get_job(self.conn, job))
                output = self.output(job)
                self.assertTrue(output['accepted'], output)
                self.assertEqual(output['analysis']['support_detail'], source)
                transmitted = json.loads(self.provider.await_args.kwargs['contents'].split('\nSOURCE: ')[1])
                self.assertEqual(transmitted['content'], source)
                self.assertEqual(output['analysis']['source_support_selection']['part_count'], 1)
            self.assertTrue(all(call.kwargs['input_tokens'] <= 6000 for call in admit.await_args_list))

    async def test_count_service_failure_waits_not_size_failure_or_tiny_generation_chunks(self):
        counter = self.setup_provider()
        counter.side_effect = RuntimeError('synthetic private provider error')
        job = self.enqueue(self.source(3000))
        self.assertEqual(await self.run_job(job), 'waiting')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'analysis_error')
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_saved_long_result_missing_selection_cannot_be_approved(self):
        self.setup_provider()
        job = self.enqueue(self.source(6500))
        self.assertEqual(await self.run_job(job), 'complete')
        result = self.output(job)
        self.assertTrue(result['accepted'])
        result['analysis'].pop('source_support_selection')
        self.conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                          (json.dumps(result), job))
        checked = self.output(job)
        self.assertFalse(checked['accepted'])
        self.assertEqual(checked['target_status'], 'review')
        self.assertEqual(checked['analysis']['support_detail'], '')

    async def test_sensitive_attachment_tail_blocks_even_count_tokens(self):
        counter = self.setup_provider()
        job = self.enqueue(self.source(8000) + '\n--- [첨부파일: 신청서]\n개인 휴대전화: 010-1234-5678')
        self.assertEqual(await self.run_job(job), 'failed')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'ai_input_review_required')
        counter.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_related_attachment_text_is_kept_but_failed_extraction_stays_review(self):
        self.setup_provider()
        source = fixtures.SHORT + '\n--- [첨부파일: 안내문]\n제외 대상: 이미 참여한 사람은 재신청 불가'
        job = self.enqueue(source, page=page_coverage.make('attachment_review'))
        self.assertEqual(await self.run_job(job), 'complete')
        result = self.output(job)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(result['target_status'], 'review')

    async def test_saved_successful_parts_survive_quota_pause_and_resume_without_recall(self):
        counter = self.setup_provider()
        counter.side_effect = lambda **kw: SimpleNamespace(total_tokens=len(kw['contents']) // 2)
        source = self.source(15500)
        job = self.enqueue(source)
        successful = self.provider.return_value
        self.provider.side_effect = [successful, quota.AiLocalBudgetExhausted('synthetic')]
        with patch.object(quota, 'acquire_model_slot', new_callable=AsyncMock):
            self.assertEqual(await self.run_job(job), 'failed')
        saved = jobs.get_job(self.conn, job)
        self.assertEqual(saved['reason'], 'local_budget_exhausted')
        state = json.loads(saved['checkpoint_json'])
        self.assertEqual(len(state['done']), 1)
        self.assertTrue(state['pending'])
        self.assertIsNone(saved['result_json'])
        completed_prompt = self.provider.await_args_list[0].kwargs['contents']
        self.provider.reset_mock(side_effect=True)
        self.provider.return_value = successful
        jobs.resume_job(self.conn, job, expected_attempts=saved['attempts'])
        with patch.object(quota, 'acquire_model_slot', new_callable=AsyncMock):
            self.assertEqual(await self.run_job(job), 'complete', jobs.get_job(self.conn, job))
        self.assertNotIn(completed_prompt, [call.kwargs['contents'] for call in self.provider.await_args_list])
        output = self.output(job)
        self.assertEqual(output['analysis']['support_detail'], source)
        self.assertEqual(output['analysis']['source_input']['status'], 'complete')
        self.assertFalse(output['accepted'])
        self.assertEqual(output['target_status'], 'review')
        self.assertIn('부분 간', output['analysis']['grounding_note'])
        self.assertGreater(output['analysis']['source_support_selection']['part_count'], 1)

    async def test_cancelled_checkpoint_write_keeps_input_and_no_final_result(self):
        self.setup_provider()
        job = self.enqueue(self.source(3000))
        original_db = self.call_db
        async def cancel_at_checkpoint(fn, *args, **kwargs):
            if fn is jobs.save_checkpoint:
                jobs.cancel_job(self.conn, job)
            return await original_db(fn, *args, **kwargs)
        self.assertEqual(await process_job(job, self.client, call_db=cancel_at_checkpoint, clock=lambda: 0), 'failed')
        saved = jobs.get_job(self.conn, job)
        self.assertEqual(saved['reason'], 'cancelled')
        self.assertIsNone(saved['result_json'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_job_cancel_during_count_prevents_generation(self):
        counter = self.setup_provider()
        job = self.enqueue(self.source(3000))
        async def cancel_during_count(**kwargs):
            jobs.cancel_job(self.conn, job)
            return SimpleNamespace(total_tokens=1800)
        counter.side_effect = cancel_during_count
        self.assertEqual(await self.run_job(job), 'failed')
        self.provider.assert_not_awaited()
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'cancelled')
        self.assertIsNone(jobs.get_job(self.conn, job)['result_json'])

    async def test_job_cancel_during_quota_wait_prevents_generation(self):
        self.setup_provider()
        job = self.enqueue(self.source(3000))
        async def cancel_during_admission(*args, **kwargs):
            jobs.cancel_job(self.conn, job)
        with patch.object(quota, 'acquire_model_slot', side_effect=cancel_during_admission):
            self.assertEqual(await self.run_job(job), 'failed')
        self.provider.assert_not_awaited()
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'cancelled')
        self.assertIsNone(jobs.get_job(self.conn, job)['result_json'])

    def test_range_partition_never_loses_newlines_tables_or_tail(self):
        source = '대상\n행1 | 조건1\n행2 | 조건2\n\n첨부\n마지막 예외조건입니다.'
        pieces = [[0, len(source)]]
        for _ in range(3):
            first = pieces.pop(0)
            pieces[0:0] = _split(source, *first)
        self.assertEqual(''.join(source[a:b] for a, b in pieces), source)

    def test_bad_checkpoint_holes_overlaps_and_changed_identity_rejected(self):
        for checkpoint in (
            dict(identity='id', done=[], pending=[[0, 2], [3, 5]]),
            dict(identity='id', done=[], pending=[[0, 3], [2, 5]]),
            dict(identity='different', done=[], pending=[[0, 5]]),
        ):
            with self.assertRaises(ValueError):
                _checkpoint(checkpoint, 'id', 'abcde')

    def test_v18_limits_and_cache_recipe_are_unchanged(self):
        self.assertFalse(custom.recipe_options(custom.SHARED_SCHEMA_RECIPE)['complete_document_rules'])
        self.assertTrue(custom.recipe_options(custom.RECIPE)['complete_document_rules'])
        self.assertFalse(selection.eligible_source('가' * 6001, extended=True))
        self.assertTrue(selection.eligible_source('가' * 15500, complete_document=True))

    async def test_v18_cached_result_does_not_satisfy_v19_job(self):
        self.setup_provider()
        old = fixtures.CurrentCustomInputBudget.enqueue(self, fixtures.SHORT, recipe=custom.SHARED_SCHEMA_RECIPE)
        new = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'], jobs.get_job(self.conn, new)['cache_key'])
        with patch.object(quota, 'acquire_model_slot', new_callable=AsyncMock):
            self.assertEqual(await self.run_job(old), 'complete')
            self.assertEqual(await self.run_job(new), 'complete')
        self.assertEqual(self.provider.await_count, 2)
        self.assertEqual(self.output(old)['analysis']['source_support_selection']['version'], 2)
        self.assertEqual(self.output(new)['analysis']['source_support_selection']['version'], 3)

    def test_conflicting_part_conditions_never_become_one_approved_range(self):
        from services.news_analysis_safety import assess
        from services.news_profile import NewsProfile
        first, second = self.response(), self.response()
        first.update(start_age_months=120, end_age_months=180, deadline='2026-10-01')
        second.update(start_age_months=240, end_age_months=300, deadline='2026-11-01',
                      applicant_type='organization', is_relevant=False)
        merged = _merge([first, second])
        self.assertIsNone(merged['start_age_months'])
        self.assertIsNone(merged['end_age_months'])
        self.assertEqual(merged['deadline'], '')
        self.assertEqual(merged['applicant_type'], 'unknown')
        output = selection.materialize(merged, fixtures.SHORT, extended=True,
                                       complete_document=True, part_count=2)
        checked = assess(NewsProfile('p', 1, '시험', '전국'), output, fixtures.SHORT,
            whole_document_rules=True, complete_document_rules=True,
            source_payload=dict(content=fixtures.SHORT, source_page=page_coverage.make()))
        self.assertFalse(checked['accepted'])
        self.assertEqual(checked['target_status'], 'review')

    def test_checkpoint_identity_is_pinned_when_default_recipe_moves(self):
        profile = SimpleNamespace(fingerprint='fixed-profile')
        identity = _identity(profile, {'content': 'fixed-source'}, self.model)
        with patch.object(custom, 'RECIPE', 'future-default'):
            self.assertEqual(_identity(profile, {'content': 'fixed-source'}, self.model), identity)

"""Regression for complete-source import and source-format holds.

Run with scripts/run_isolated_tests.py: synthetic data, fake provider, no export
requests or operational profile. Local payload construction is not an upload.
"""
import html
import json
import unittest
from unittest.mock import AsyncMock, patch

from services import news_analysis_store as jobs, news_result_import as imports
from services import news_unified_feed as feed, news_default_condition as defaults
from services import news_run_executor as executor, news_workflow_progress as progress
from services import notion_service, news_gallery, news_source_selection as selection
from services import ai_coordinator as quota
from tests import test_news_complete_analysis as complete_fixtures


class NewsResidualAudit(unittest.IsolatedAsyncioTestCase):
    setUp = complete_fixtures.CompleteNewsInput.setUp
    call_db = complete_fixtures.CompleteNewsInput.call_db
    response = complete_fixtures.CompleteNewsInput.response
    output = complete_fixtures.CompleteNewsInput.output
    enqueue = complete_fixtures.CompleteNewsInput.enqueue
    setup_provider = complete_fixtures.CompleteNewsInput.setup_provider
    source = complete_fixtures.CompleteNewsInput.source
    run_job = complete_fixtures.CompleteNewsInput.run_job

    async def assert_complete_source_roundtrip(self, size):
        self.setup_provider()
        prefix = '청소년 대상 교육비 지원\n개인이 신청합니다.\n'
        tail = '\n마지막 제외 조건: 같은 사업의 지원금은 중복 신청할 수 없습니다.'
        middle_size = size - len(prefix) - len(tail)
        source = prefix + ('안내😀' * (middle_size // 3 + 1))[:middle_size] + tail
        self.assertEqual(len(source), size)
        job = self.enqueue(source)
        with patch.object(quota, 'acquire_model_slot', new_callable=AsyncMock):
            self.assertEqual(await self.run_job(job), 'complete')
        output = self.output(job)
        self.assertTrue(output['accepted'])
        self.assertEqual(output['analysis']['support_detail'], source)
        self.assertEqual(output['analysis']['source_input']['status'], 'complete')
        self.assertEqual(imports.eligibility(self.conn, defaults.PROFILE_ID, job),
                         {'allowed': True, 'reason': 'confirmation_required'})
        details = jobs.get_profile_result_details(self.conn, defaults.PROFILE_ID, 'job:' + job)
        self.assertEqual(details['details']['support_detail'], source)
        row = next(item for item in feed.read_feed(self.conn, defaults.PROFILE_ID, view='ready')['items']
                   if item['job_id'] == job)
        self.assertTrue(row['selectable'])
        self.assertEqual(row['support_detail'], source)
        preview = imports.preview(self.conn, defaults.PROFILE_ID, job)
        with self.assertRaisesRegex(ValueError, 'Explicit confirmation'):
            imports.import_result(self.conn, defaults.PROFILE_ID, job,
                                  fingerprint=preview['fingerprint'], confirmed=False)
        with self.assertRaisesRegex(ValueError, 'Preview changed'):
            imports.import_result(self.conn, defaults.PROFILE_ID, job,
                                  fingerprint='0' * 64, confirmed=True)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 0)
        result = imports.import_result(self.conn, defaults.PROFILE_ID, job,
                                       fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(result['status'], 'created')
        cursor = self.conn.execute('SELECT * FROM welfare_items WHERE id=?', (result['item_id'],))
        item = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
        self.assertEqual(item['support_detail'], source)
        self.assertEqual(json.loads(item['ai_data'])['support_detail'], source)
        stored_row = next(item for item in feed.read_feed(self.conn, defaults.PROFILE_ID, view='ready')['items']
                          if item.get('item_id') == result['item_id'])
        self.assertEqual(stored_row['support_detail'], source)
        self.assertIn(html.escape(source, quote=True), news_gallery._detail_html(item))
        self.assertIn(source, news_gallery._copy_text(item))
        payload = notion_service.build_payload(item, '', 'offline-database')
        blocks = [block for callout in payload['children'] for block in callout['callout']['children']]
        lines = [''.join(run['text']['content'] for run in block[block['type']].get('rich_text', []))
                 for block in blocks]
        self.assertEqual(lines[-len(source.splitlines()):], source.splitlines())
        self.assertLessEqual(len(json.dumps(payload).encode('utf-8')), 500_000)

    async def test_above_old_16000_limit_saves_and_exports_without_losing_tail(self):
        await self.assert_complete_source_roundtrip(17000)

    async def test_30000_source_boundary_saves_and_exports_without_losing_tail(self):
        await self.assert_complete_source_roundtrip(selection.COMPLETE_SOURCE_CHARS)

    async def test_short_source_format_holds_are_not_size_or_quota_failures(self):
        counter = self.setup_provider()
        for malformed in ('<div>지원 내용 안내</div>', '[... 일부 내용 생략 ...]'):
            with self.subTest(malformed=malformed):
                source = '청소년 대상 교육비 지원\n개인이 신청합니다.\n' + malformed
                job = self.enqueue(source)
                self.assertEqual(await self.run_job(job), 'failed')
                stored = jobs.get_job(self.conn, job)
                self.assertEqual(stored['reason'], 'ai_source_review_required')
                self.assertEqual(json.loads(stored['input_json'])['content'], source)
                counter.assert_not_awaited()
                self.provider.assert_not_awaited()
                row = next(item for item in feed.read_feed(self.conn, defaults.PROFILE_ID, view='review')['items']
                           if item['job_id'] == job)
                self.assertFalse(row['selectable'])
                self.assertEqual(row['review_reason'], 'ai_source_review_required')
                self.assertIn('본문 형식', row['input_note'])
                self.assertNotIn('전송 보호값', row['input_note'])
                listing = next(item for item in jobs.list_profile_results(self.conn, defaults.PROFILE_ID)['items']
                               if item['job_id'] == job)
                self.assertEqual(listing['review_reason'], 'ai_source_review_required')
                run_id = stored['run_id']
                self.assertIsNone(executor._existing_failure(self.conn, run_id))
                self.assertEqual(executor._review_summary(self.conn, run_id)['review_reasons'],
                                 {'ai_source_review_required': 1})
                counts = progress.analysis_progress(self.conn, run_id)
                self.assertEqual(counts['analysis_processed'], 1)
                self.assertEqual(counts['analysis_review_required'], 1)
                self.assertEqual(counts['analysis_remaining'], 0)
                self.assertEqual(counts['analysis_failed'], 0)

    async def test_actual_source_length_over_limit_still_has_size_hold(self):
        counter = self.setup_provider()
        source = '가' * (selection.COMPLETE_SOURCE_CHARS + 1)
        job = self.enqueue(source)
        self.assertEqual(await self.run_job(job), 'failed')
        stored = jobs.get_job(self.conn, job)
        self.assertEqual(stored['reason'], 'ai_input_size_review_required')
        self.assertEqual(json.loads(stored['input_json'])['content'], source)
        counter.assert_not_awaited()
        self.provider.assert_not_awaited()

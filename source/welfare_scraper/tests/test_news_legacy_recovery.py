"""Offline failure -> new analysis -> explicit selected-save recovery."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from services import news_analysis_store as jobs, news_profile_store as profiles
from services import news_unified_feed as feed, legacy_news_analysis, news_custom_analysis
from services import news_source_selection, page_coverage
from services.news_analysis_worker import process_job
from tests import test_news_unified_feed as fixture


class TestLegacyNewsRecovery(unittest.TestCase):
    setUp = fixture.TestUnifiedNewsFeed.setUp
    tearDown = fixture.TestUnifiedNewsFeed.tearDown
    new_job = fixture.TestUnifiedNewsFeed.new_job
    listing = fixture.TestUnifiedNewsFeed.listing
    prepare = fixture.TestUnifiedNewsFeed.prepare

    def failed_item(self, *, link=None):
        value = legacy_news_analysis.failed_result(reason='authentication_required', attempts=1)
        encoded = json.dumps(value)
        with database.get_connection() as conn:
            cursor = conn.execute('''INSERT INTO welfare_items(title,link,ai_data,ai_skip_reason)
                VALUES (?,?,?,?)''', ('이전 연결 실패', link or self.payload['link'], encoded,
                                     'analysis_review_required'))
            conn.commit()
            return cursor.lastrowid, encoded

    def saved(self, item_id):
        with database.get_connection() as conn:
            return dict(conn.execute('SELECT * FROM welfare_items WHERE id=?', (item_id,)).fetchone())

    def test_ready_result_and_explicit_save_recover_same_id_with_history(self):
        item_id, encoded = self.failed_item()
        later = self.new_job()
        with database.get_connection() as conn:
            before = conn.total_changes
            visible = feed.read_feed(conn, 'p')
            self.assertEqual(conn.total_changes, before)
        self.assertEqual(visible['total'], 1)
        row = visible['items'][0]
        self.assertEqual(row['job_id'], later)
        self.assertEqual(row['recovery_item_id'], item_id)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)
        self.assertFalse(legacy_news_analysis.backlog_due(self.saved(item_id)))
        response = self.prepare([row])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['ids'], [item_id])
        self.assertFalse(response.json()['ai_called'])
        self.assertFalse(response.json()['notion_uploaded'])
        recovered = self.saved(item_id)
        self.assertIsNone(recovered['ai_skip_reason'])
        self.assertTrue(json.loads(recovered['ai_data'])['is_relevant'])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], 1)
            snapshots = conn.execute('SELECT snapshot_data FROM welfare_item_snapshots').fetchall()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(snapshots[0][0])['ai_data'], encoded)
            self.assertEqual(conn.execute('SELECT outcome FROM news_result_imports').fetchone()[0], 'created')
        stored = self.listing()['items'][0]
        self.assertEqual(stored['item_id'], item_id)
        self.assertEqual(self.prepare([stored]).json()['ids'], [item_id])

    def test_later_current_worker_analysis_reaches_selected_save(self):
        from services import ai_rate_policy, ai_verified_input
        item_id, encoded = self.failed_item()
        payload = self.payload | {'content': self.payload['content'] + '\n복구 후 새 안내입니다.',
                                  'source_page': page_coverage.make()}
        facts = copy.deepcopy(self.facts)
        facts.update(support_detail=payload['content'], contact='', deadline='',
                     source_page=payload['source_page'], source_input={'status': 'complete'})
        facts['fact_evidence'] = {'support_detail': payload['content'],
            'eligibility_detail': '원문 자격 확인', 'contact': '', 'deadline': '',
            'is_relevant': '돌봄 지원'}
        # v19 replies select the source; only the local worker may hydrate the
        # full prose and provenance metadata after validation.
        for key in ('support_detail', 'source_page', 'source_input', 'secret'):
            facts.pop(key, None)
        facts['fact_evidence'].pop('support_detail')
        facts['_support_source_units'] = [1]
        with database.get_connection() as conn:
            run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='시험 기관', url='https://example.invalid')])
            later = jobs.enqueue(conn, run, payload=payload, model=ai_rate_policy.MODELS[0], recipe=news_custom_analysis.RECIPE)
        async def call_db(fn, *args, **kwargs):
            with database.get_connection() as conn:
                return fn(conn, *args, **kwargs)
        # The response is synthetic; worker validation, grounding, persistence,
        # feed, fingerprint preparation and item materialization remain real.
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        with patch.object(ai_verified_input, '_take_count_slot', new=AsyncMock(return_value=True)), \
             patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts) as classify:
            self.assertEqual(asyncio.run(process_job(later, client, call_db=call_db)), 'complete')
            classify.assert_awaited_once()
        visible = self.listing()
        self.assertEqual(visible['total'], 1)
        self.assertEqual(visible['items'][0]['job_id'], later)
        response = self.prepare(visible['items'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['ids'], [item_id])
        self.assertNotEqual(self.saved(item_id)['ai_data'], encoded)

    def test_user_archive_dismissal_and_notion_link_block_recovery_and_stale_save(self):
        item_id, encoded = self.failed_item()
        for column, value in (('is_archived', 1), ('notion_dismissed', 1), ('notion_page_id', 'saved-page')):
            with self.subTest(column=column):
                row = self.listing()['items'][0]
                with database.get_connection() as conn:
                    conn.execute(f'UPDATE welfare_items SET {column}=? WHERE id=?', (value, item_id))
                    conn.commit()
                self.assertEqual(self.listing()['total'], 0)
                self.assertEqual(self.prepare([row]).status_code, 409)
                self.assertEqual(self.saved(item_id)['ai_data'], encoded)
                with database.get_connection() as conn:
                    conn.execute(f'UPDATE welfare_items SET {column}=NULL WHERE id=?', (item_id,))
                    conn.commit()

    def test_imported_choice_and_uncertain_upload_remain_protected(self):
        item_id, encoded = self.failed_item()
        with database.get_connection() as conn:
            conn.execute('''INSERT INTO news_result_imports(job_id,fingerprint,item_id,link,outcome)
                VALUES (?,?,?,?, 'existing')''', (self.job, 'saved-choice', item_id, self.payload['link']))
            conn.commit()
        self.new_job()
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)

    def test_upload_attempt_without_receipt_blocks_recovery(self):
        item_id, encoded = self.failed_item()
        with database.get_connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS notion_upload_attempts
                (link TEXT PRIMARY KEY, page_id TEXT)''')
            conn.execute('INSERT INTO notion_upload_attempts(link,page_id) VALUES (?,NULL)',
                         (self.payload['link'],))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)

    def test_changed_failure_invalidates_feed_selection_without_writes(self):
        item_id, _encoded = self.failed_item()
        row = self.listing()['items'][0]
        replacement = json.dumps(legacy_news_analysis.failed_result(reason='daily_quota_exhausted', attempts=1))
        with database.get_connection() as conn:
            conn.execute('UPDATE welfare_items SET ai_data=? WHERE id=?', (replacement, item_id))
            conn.commit()
        self.assertEqual(self.prepare([row]).status_code, 409)
        self.assertEqual(self.saved(item_id)['ai_data'], replacement)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_item_snapshots').fetchone()[0], 0)

    def test_audit_receipt_failure_rolls_back_snapshot_and_replacement(self):
        item_id, encoded = self.failed_item()
        row = self.listing()['items'][0]
        with database.get_connection() as conn:
            conn.execute("""CREATE TRIGGER reject_recovery BEFORE INSERT ON news_result_imports
                BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END""")
            conn.commit()
        self.assertEqual(self.prepare([row]).status_code, 503)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_item_snapshots').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM news_result_imports').fetchone()[0], 0)

    def test_canonical_url_recovers_without_changing_original_link(self):
        link = self.payload['link'] + '#old-source'
        item_id, _encoded = self.failed_item(link=link)
        row = self.listing()['items'][0]
        self.assertEqual(self.prepare([row]).status_code, 200)
        self.assertEqual(self.saved(item_id)['link'], link)
        self.assertEqual(self.listing()['items'][0]['item_id'], item_id)

    def test_ambiguous_stored_urls_and_successful_undated_facts_are_not_recovered(self):
        item_id, encoded = self.failed_item()
        self.failed_item(link=self.payload['link'] + '#duplicate')
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)
        with database.get_connection() as conn:
            conn.execute('DELETE FROM welfare_items WHERE id<>?', (item_id,))
            conn.execute('UPDATE welfare_items SET ai_data=?,ai_skip_reason=NULL WHERE id=?',
                (json.dumps({'is_relevant': True, 'category': '복지', 'target': '일반', 'item_type': 'program'}), item_id))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)

    def test_no_resume_promise_for_failed_legacy_without_a_workflow(self):
        item_id, _encoded = self.failed_item()
        response = self.client.get('/api/news/feed', params={'view': 'processing'})
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()['items'][0]
        self.assertEqual(row['item_id'], item_id)
        self.assertIsNone(row['workflow_id'])
        self.assertEqual(row['recovery_action'], 'new_collection')
        self.assertIn('바로 재개할 연결 작업이 없습니다', row['input_note'])
        self.assertIn('직접 실행', row['input_note'])
        self.assertNotIn('다음 수집에서 다시 분석합니다', row['input_note'])

    def test_only_explicit_quota_pending_marker_can_recover_empty_facts(self):
        with database.get_connection() as conn:
            cursor = conn.execute('INSERT INTO welfare_items(title,link) VALUES (?,?)',
                                  ('수집된 원문', self.payload['link']))
            item_id = cursor.lastrowid
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        with database.get_connection() as conn:
            conn.execute("UPDATE welfare_items SET ai_skip_reason='quota' WHERE id=?", (item_id,))
            conn.commit()
        rows = self.listing()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.prepare(rows).json()['ids'], [item_id])

    def test_failed_permanent_marker_is_never_recovered(self):
        item_id, encoded = self.failed_item()
        with database.get_connection() as conn:
            conn.execute("UPDATE welfare_items SET ai_skip_reason='failed_permanent' WHERE id=?", (item_id,))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.saved(item_id)['ai_data'], encoded)

    def test_input_size_hold_explains_preservation_and_explicit_new_collection(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='ai_input_size_review_required' WHERE job_id=?",
                         (self.job,))
            conn.commit()
        result = self.listing('review')
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertFalse(row['selectable'])
        self.assertFalse(result['ai_called'])
        self.assertIn('입력이 현재 전송 보호값을 초과', row['input_note'])
        self.assertIn('원문은 보존됩니다', row['input_note'])
        self.assertIn('상세 출처와 추가 AI 요청 문구', row['input_note'])
        self.assertIn('새 수집을 시작해주세요', row['input_note'])
        self.assertNotIn('한도를 높', row['input_note'])

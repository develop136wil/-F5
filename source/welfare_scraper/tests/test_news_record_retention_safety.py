"""Independent retention/feed safety regressions against disposable records."""
import datetime as dt
import unittest

import database
from services import news_analysis_store as jobs
from services import news_record_retention as retention, news_unified_feed as feed
from tests import test_news_result_import as fixture


class TestRetentionFeedSafety(unittest.TestCase):
    setUp = fixture.TestNewsResultImport.setUp
    tearDown = fixture.TestNewsResultImport.tearDown
    new_job = fixture.TestNewsResultImport.new_job
    now = dt.datetime(2030, 9, 17, tzinfo=dt.timezone.utc)
    old = '2030-07-01 00:00:00'

    def make_old(self, conn):
        conn.execute('UPDATE news_run_snapshots SET created_at=?', (self.old,))
        conn.execute('UPDATE news_job_events SET created_at=?', (self.old,))
        for run, in conn.execute('SELECT run_id FROM news_run_snapshots').fetchall():
            conn.execute('''INSERT OR REPLACE INTO news_profile_workflows
                (workflow_id,run_id,state,stage,updated_at) VALUES (?,?, 'failed','analysis',?)''',
                ('workflow-' + run, run, self.old))
        conn.commit()

    def projection(self, conn):
        return [(row['id'], row['view'], row['selectable']) for row in feed._read_all(conn, 'p')]

    def test_hidden_valid_result_and_newest_failure_stay_without_resurrection(self):
        latest = self.new_job(payload=self.payload | {'title': '같은 원문의 재수집'})
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',result_json=NULL,reason='analysis_error' WHERE job_id=?", (latest,))
            self.make_old(conn)
            before = self.projection(conn)
            self.assertEqual(before, [('job:' + latest, 'processing', False)])
            result = retention.cleanup(conn, 7, now=self.now)
            self.assertEqual(result['deleted_jobs'], 0)
            self.assertEqual(result['skipped_resurrection'], 1)
            self.assertGreaterEqual(result['skipped_protected'], 1)
            self.assertEqual(self.projection(conn), before)
            self.assertEqual(conn.execute('SELECT count(*) FROM news_analysis_jobs').fetchone()[0], 2)
            # A repeated cleanup must not gradually peel the suppression stack.
            self.assertEqual(retention.cleanup(conn, 7, now=self.now)['deleted_jobs'], 0)
            self.assertEqual(self.projection(conn), before)

    def test_unknown_age_older_row_keeps_the_newest_excluded_suppression_row(self):
        latest = self.new_job(payload=self.payload | {'title': '이전 자료가 아닌 메뉴 재확인'})
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',result_json=NULL,reason='analysis_error'")
            self.make_old(conn)
            older_run = conn.execute('SELECT run_id FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone()[0]
            latest_run = conn.execute('SELECT run_id FROM news_analysis_jobs WHERE job_id=?', (latest,)).fetchone()[0]
            conn.execute('UPDATE news_profile_workflows SET updated_at=? WHERE run_id=?', ('unknown', older_run))
            conn.execute('''INSERT INTO news_run_candidates
                (run_id,candidate_key,site_id,payload_json,route,job_id) VALUES (?,?,?,?,?,?)''',
                (latest_run, 'excluded-latest', '1', jobs._encode(self.payload | {'selection_reason': 'not_article'}), 'excluded', latest))
            conn.commit()
            before = self.projection(conn)
            self.assertEqual(before, [('job:' + latest, 'records', False)])
            result = retention.cleanup(conn, 30, now=self.now)
            self.assertEqual(result['deleted_jobs'], 0)
            self.assertEqual(result['deleted_candidates'], 0)
            self.assertGreaterEqual(result['skipped_unknown_date'], 1)
            self.assertEqual(result['skipped_resurrection'], 2)
            self.assertEqual(self.projection(conn), before)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_active_cross_run_candidate_protects_the_linked_old_job(self):
        active_job = self.new_job(payload=self.payload | {'link': 'https://example.invalid/active'})
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',result_json=NULL,reason='analysis_error'")
            self.make_old(conn)
            active_run = conn.execute('SELECT run_id FROM news_analysis_jobs WHERE job_id=?', (active_job,)).fetchone()[0]
            conn.execute("UPDATE news_profile_workflows SET state='running' WHERE run_id=?", (active_run,))
            conn.execute('''INSERT INTO news_run_candidates
                (run_id,candidate_key,site_id,payload_json,route,job_id) VALUES (?,?,?,?,?,?)''',
                (active_run, 'cross-run-reference', '1', jobs._encode(self.payload), 'matched', self.job))
            conn.commit()
            result = retention.cleanup(conn, 7, now=self.now)
            self.assertEqual(result['deleted_jobs'], 0)
            self.assertEqual(result['deleted_candidates'], 0)
            self.assertGreaterEqual(result['skipped_active'], 2)
            self.assertGreaterEqual(result['skipped_protected'], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM news_analysis_jobs').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unknown_future_or_recent_activity_prevents_old_candidate_deletion(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',result_json=NULL,reason='analysis_error'")
            self.make_old(conn)
            # Completing this historical fixture does not emit a worker event;
            # create one explicitly so the mutation really exercises its age.
            conn.execute('''INSERT INTO news_job_events(job_id,event,attempts,created_at)
                VALUES (?,'fixture-activity',0,?)''', (self.job, self.old))
            conn.commit()
            for value in ('unknown', '2031-01-01 00:00:00', '2030-09-16 00:00:00'):
                with self.subTest(last_activity=value):
                    conn.execute('UPDATE news_job_events SET created_at=?', (value,))
                    conn.commit()
                    result = retention.cleanup(conn, 7, now=self.now)
                    self.assertEqual(result['deleted_jobs'], 0)
                    self.assertEqual(conn.execute('SELECT count(*) FROM news_analysis_jobs').fetchone()[0], 1)
            conn.execute('DELETE FROM news_profile_workflows')
            conn.execute('UPDATE news_job_events SET created_at=?', (self.old,))
            conn.commit()
            result = retention.cleanup(conn, 7, now=self.now)
            self.assertEqual(result['deleted_jobs'], 0)
            self.assertEqual(result['skipped_unknown_date'], 1)

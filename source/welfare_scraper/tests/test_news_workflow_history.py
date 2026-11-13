import sqlite3
import unittest
from unittest.mock import patch
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services import news_profile_workflow as workflows
from services.news_workflow_history import clear_history


class TestWorkflowHistory(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn);jobs.ensure_schema(self.conn)
        for identity in ('p','other'):
            profiles.create_profile(self.conn,NewsProfile(identity,1,'시험','전국',site_ids=('1',)))
        self.addCleanup(self.conn.close)

    def make(self,state='complete',profile='p',reason=None):
        run=profiles.prepare_run(self.conn,profile,expected_revision=1,sites=[dict(id=1,name='시험',url='https://example.invalid')])
        identity=workflows.create_workflow(self.conn,run)
        self.conn.execute("UPDATE news_profile_workflows SET state=?,stage='finished',error_code=?,updated_at='2026-08-01 00:00:00' WHERE workflow_id=?",(state,reason,identity))
        return identity,run

    def test_terminal_history_hidden_but_source_data_and_dates_preserved(self):
        identity,run=self.make()
        job=jobs.enqueue(self.conn,run,payload=dict(title='지원',link='https://example.invalid/post',content='보존할 본문',site_name='시험'),model='m',recipe='test')
        before=self.conn.execute('SELECT * FROM news_analysis_jobs').fetchall()
        snapshot=self.conn.execute('SELECT * FROM news_run_snapshots').fetchall()
        self.assertEqual(clear_history(self.conn,'p'),{'cleared':1,'news_deleted':False})
        self.assertEqual(workflows.list_workflows(self.conn,'p'),[])
        self.assertEqual(workflows.get_workflow(self.conn,identity)['updated_at'],'2026-08-01 00:00:00')
        self.assertEqual(self.conn.execute('SELECT * FROM news_analysis_jobs').fetchall(),before)
        self.assertEqual(self.conn.execute('SELECT * FROM news_run_snapshots').fetchall(),snapshot)
        self.assertEqual(jobs.get_job(self.conn,job)['run_id'],run)
        self.assertEqual(clear_history(self.conn,'p')['cleared'],0)
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_running_waiting_claimed_and_protected_never_clear(self):
        keep=[self.make(state)[0] for state in ('ready','running','waiting')]
        claimed,run=self.make()
        job=jobs.enqueue(self.conn,run,payload=dict(title='지원',link='https://example.invalid/claim',content='본문',site_name='시험'),model='m',recipe='test')
        jobs.claim(self.conn,job,now=0)
        protected,_=self.make()
        self.assertEqual(clear_history(self.conn,'p',protected_ids=[protected])['cleared'],0)
        self.assertEqual({item['workflow_id'] for item in workflows.list_workflows(self.conn,'p')},set(keep+[claimed,protected]))

    def test_single_profile_scope_and_bulk_includes_more_than_visible_twenty(self):
        other,_=self.make(profile='other')
        ids=[self.make('cancelled')[0] for _ in range(25)]
        self.assertEqual(clear_history(self.conn,'p',workflow_ids=[ids[0],other])['cleared'],1)
        self.assertEqual(clear_history(self.conn,'p')['cleared'],24)
        self.assertEqual(len(workflows.list_workflows(self.conn,'other')),1)

    def test_failed_resumable_is_protected_and_transaction_can_roll_back(self):
        identity,_=self.make('failed')
        original=workflows.get_workflow
        with patch('services.news_workflow_history.get_workflow',side_effect=lambda conn,key:{**original(conn,key),'can_clear_history':False}):
            self.assertEqual(clear_history(self.conn,'p')['cleared'],0)
        self.conn.commit()
        self.conn.execute('BEGIN')
        self.assertEqual(clear_history(self.conn,'p')['cleared'],1)
        self.conn.rollback()
        self.assertEqual(workflows.list_workflows(self.conn,'p')[0]['workflow_id'],identity)

    def test_upgrade_adds_only_default_visible_column(self):
        identity,_=self.make()
        self.conn.execute('ALTER TABLE news_profile_workflows DROP COLUMN history_dismissed')
        self.assertEqual(workflows.list_workflows(self.conn,'p')[0]['workflow_id'],identity)
        self.assertEqual(self.conn.execute('SELECT history_dismissed FROM news_profile_workflows').fetchone()[0],0)

    def test_retired_source_waiting_can_clear_but_current_waiting_cannot(self):
        identity,_=self.make('waiting')
        with patch.object(workflows,'retired_source_scope',return_value=True):
            self.assertTrue(workflows.get_workflow(self.conn,identity)['can_clear_history'])
            self.assertEqual(clear_history(self.conn,'p')['cleared'],1)
        current,_=self.make('waiting')
        self.assertFalse(workflows.get_workflow(self.conn,current)['can_clear_history'])
        self.assertEqual(clear_history(self.conn,'p')['cleared'],0)

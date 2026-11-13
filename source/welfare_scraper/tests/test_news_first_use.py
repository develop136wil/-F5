"""Offline first-use contract checks; caller supplies a disposable SQLite DB."""
import sqlite3
import unittest
from unittest.mock import patch

from services import news_default_condition as defaults
from services import news_profile_store as store
from services.news_profile import capture_legacy_profile


class FirstUseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''CREATE TABLE sites(id INTEGER PRIMARY KEY);
            CREATE TABLE welfare_items(id INTEGER PRIMARY KEY, ai_data TEXT);
            CREATE TABLE keywords(keyword TEXT);
            CREATE TABLE excluded_keywords(keyword TEXT);''')

    def test_registered_sites_and_saved_keys_do_not_authorize_legacy_or_seed_infant_defaults(self):
        self.conn.execute('INSERT INTO sites VALUES(1)')
        self.conn.execute("INSERT INTO keywords VALUES('영유아')")
        self.conn.execute("INSERT INTO excluded_keywords VALUES('고시공고')")
        before=self.conn.total_changes
        self.assertFalse(defaults.legacy_collection_available(self.conn))
        self.assertEqual(defaults.initial_settings(self.conn,{'GOOGLE_API_KEY':'do-not-return',
            'NOTION_TOKEN':'do-not-return'}),{})
        self.assertIsNone(defaults.read(self.conn))
        self.assertEqual(self.conn.total_changes,before)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='news_profiles'").fetchone())

    def test_explicit_legacy_capture_and_actual_scrape_history_are_positive_evidence(self):
        self.conn.execute('CREATE TABLE scrape_runs(id INTEGER PRIMARY KEY)')
        self.assertFalse(defaults.legacy_collection_available(self.conn))
        self.conn.execute('INSERT INTO scrape_runs VALUES(1)')
        self.assertTrue(defaults.legacy_collection_available(self.conn))
        self.conn.execute('DELETE FROM scrape_runs')
        store.ensure_schema(self.conn)
        profile=capture_legacy_profile(region='부산',max_age_months=144,keywords=(),
            excluded_keywords=(),site_ids=('1',),ai_prompt='',semantic_text='',relevance_enabled=True)
        store.initialize_legacy(self.conn,profile)
        before=self.conn.total_changes
        self.assertTrue(defaults.legacy_collection_available(self.conn))
        self.assertEqual(self.conn.total_changes,before)

    def test_only_unadopted_legacy_program_history_counts(self):
        self.conn.execute('CREATE TABLE news_result_imports(item_id INTEGER)')
        for value in ('{}','bad json','{"item_type":"manual_news"}','{"item_type":"policy"}'):
            self.conn.execute('INSERT INTO welfare_items(ai_data) VALUES(?)',(value,))
        self.assertFalse(defaults.legacy_collection_available(self.conn))
        self.conn.execute('INSERT INTO welfare_items VALUES(20,?)',('{"item_type":"program"}',))
        self.assertTrue(defaults.legacy_collection_available(self.conn))
        self.conn.execute('INSERT INTO news_result_imports VALUES(20)')
        self.assertFalse(defaults.legacy_collection_available(self.conn))

    def test_explicit_old_settings_are_previewed_without_being_adopted(self):
        self.conn.execute("INSERT INTO keywords VALUES('청소년')")
        self.conn.execute("INSERT INTO excluded_keywords VALUES('채용')")
        before=self.conn.total_changes
        result=defaults.initial_settings(self.conn,{'TARGET_REGION':'부산광역시','TARGET_AGE_MAX_MONTHS':'144',
            'CUSTOM_AI_PROMPT':'신청 방법 보존','RELEVANCE_FILTER_ENABLED':'0','GOOGLE_API_KEY':'secret'},legacy=True)
        self.assertEqual(result,{'region':'부산광역시','min_age_months':0,'max_age_months':144,
            'ai_prompt':'신청 방법 보존','relevance_enabled':False,'keywords':['청소년'],'excluded_keywords':['채용']})
        self.assertEqual(self.conn.total_changes,before)
        self.assertIsNone(defaults.read(self.conn))

    def test_only_confirmed_existing_usage_gets_its_effective_old_defaults(self):
        self.assertEqual(defaults.initial_settings(self.conn,{}),{})
        result=defaults.initial_settings(self.conn,{},legacy=True,default_keywords=('지원',),default_excluded=('채용',))
        self.assertEqual((result['region'],result['min_age_months'],result['max_age_months']),('도봉',0,108))
        self.assertEqual(result['keywords'],['지원'])


class FirstUseEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_contract_is_read_only_and_exposes_no_credentials(self):
        from services import news_profiles_api as api
        import utils
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE sites(id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO sites VALUES(1)')
        async def execute(fn):return fn(conn)
        before=conn.total_changes
        with patch.object(api,'_execute',side_effect=execute),patch.object(utils,'load_config_file',return_value={
                'GOOGLE_API_KEY':'secret','TARGET_REGION':'부산광역시','TARGET_AGE_MAX_MONTHS':'144'}):
            result=await api.get_default_condition()
        self.assertEqual(result,{'profile':None,'legacy_collection_available':False,
            'initial_settings':{'region':'부산광역시','min_age_months':0,'max_age_months':144}})
        self.assertEqual(conn.total_changes,before)
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='news_profiles'").fetchone())

import unittest
from services.news_profile import NewsProfile
from services.news_auto_sources import automatic_sources
from services.news_target_rules import evaluate_target


class AutoSourcesTests(unittest.TestCase):
    def profile(self, **values):
        return NewsProfile(profile_id='test',revision=1,name='테스트',region='부산광역시',**values)

    def test_official_aliases_never_expand_registered_sources(self):
        for field, value in [('life_cycles','노인'),('life_cycles','영유아'),('characteristics','다문화')]:
            with self.subTest(value=value):
                result=automatic_sources(self.profile(**{field:(value,)},site_ids=('1',)))
                self.assertEqual(result.sources,('sites',))
                self.assertEqual(result.site_ids,('1',))
                self.assertEqual(result.bokjiro_mode,'unsupported')
                self.assertEqual(automatic_sources(result),result)

    def test_no_official_target_never_becomes_unbounded_query(self):
        for values in ({},{'characteristics':('일반',)},{'life_cycles':('자체 대상',)}):
            self.assertEqual(automatic_sources(self.profile(**values)).sources,('sites',))

    def test_normalization_removes_old_automatic_source_without_changing_condition(self):
        original=self.profile(life_cycles=('노년',),sources=('bokjiro',),bokjiro_mode='official_tags_v1')
        self.assertEqual(original.sources,('bokjiro',))
        updated=automatic_sources(original)
        self.assertEqual(updated.sources,('sites',))
        self.assertEqual(updated.bokjiro_mode,'unsupported')
        self.assertEqual(updated.life_cycles,original.life_cycles)
        self.assertEqual(updated.site_ids,())

    def test_unknown_residence_and_age_require_review(self):
        profile=automatic_sources(self.profile(life_cycles=('아동',),min_age_months=72,max_age_months=120))
        decision=evaluate_target(profile,{'life_cycle':['아동'],'residence':[]})
        self.assertEqual(decision.status,'review')
        self.assertEqual(dict(decision.checks)['region'],'review')
        self.assertEqual(dict(decision.checks)['age'],'review')

    def test_upgrade_is_idempotent_and_preserves_old_versions(self):
        import sqlite3
        from services import news_profile_store as store
        from services.news_auto_sources import upgrade_saved_sources
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        store.ensure_schema(conn)
        original=self.profile(life_cycles=('노년',),site_ids=('7',),
                              sources=('sites','bokjiro'),bokjiro_mode='official_tags_v1')
        store.create_profile(conn,original)
        run_id=store.prepare_run(conn,'test',expected_revision=1,
                                sites=[dict(id=7,name='기관',url='https://example.invalid')])
        snapshot=conn.execute('SELECT * FROM news_run_snapshots').fetchall()
        upgrade_saved_sources(conn)
        self.assertEqual(store.get_profile(conn,'test').sources,('sites',))
        self.assertEqual(store.get_profile(conn,'test',1),original)
        self.assertEqual(conn.execute('SELECT * FROM news_run_snapshots').fetchall(),snapshot)
        self.assertEqual(store.get_run_snapshot(conn,run_id)['profile']['sources'],['sites','bokjiro'])
        upgrade_saved_sources(conn)
        self.assertEqual(store.get_profile(conn,'test').revision,2)

    def test_already_sites_only_profile_does_not_gain_revision(self):
        import sqlite3
        from services import news_profile_store as store
        from services.news_auto_sources import upgrade_saved_sources
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        store.ensure_schema(conn)
        original=self.profile(life_cycles=('노년',),site_ids=('7',))
        store.create_profile(conn,original)
        upgrade_saved_sources(conn)
        self.assertEqual(store.get_profile(conn,'test'),original)

    def test_clone_archived_source_does_not_restore_automatic_collection(self):
        import sqlite3
        from services import news_profile_store as store
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        store.ensure_schema(conn)
        original=self.profile(life_cycles=('노년',),site_ids=('7',),
                              sources=('sites','bokjiro'),bokjiro_mode='official_tags_v1')
        store.create_profile(conn,original)
        store.archive_profile(conn,'test')
        clone=store.clone_profile(conn,'test',new_id='new',name='복사본')
        self.assertEqual(clone.sources,('sites',))
        self.assertEqual(clone.site_ids,original.site_ids)
        self.assertEqual(store.get_profile(conn,'test'),original)

    def test_default_upgrade_uses_exact_registered_sites_and_preserves_saved_results(self):
        import sqlite3
        from dataclasses import replace
        from services import news_profile_store as store
        from services.news_auto_sources import upgrade_saved_sources
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        store.ensure_schema(conn)
        conn.execute('CREATE TABLE sites(id INTEGER PRIMARY KEY)')
        conn.executemany('INSERT INTO sites VALUES (?)',[(2,),(9,)])
        conn.execute('CREATE TABLE welfare_items(id INTEGER PRIMARY KEY,title TEXT)')
        conn.execute("INSERT INTO welfare_items VALUES(1,'보존할 기존 소식')")
        original=replace(self.profile(life_cycles=('영유아',),sources=('bokjiro',),
                         bokjiro_mode='official_tags_v1'),profile_id='default-news')
        store.create_profile(conn,original)
        upgrade_saved_sources(conn)
        current=store.get_profile(conn,'default-news')
        self.assertEqual(current.sources,('sites',))
        self.assertEqual(current.site_ids,('2','9'))
        self.assertEqual(store.get_profile(conn,'default-news',1),original)
        self.assertEqual(conn.execute('SELECT * FROM welfare_items').fetchall(),[(1,'보존할 기존 소식')])
        upgrade_saved_sources(conn)
        self.assertEqual(store.get_profile(conn,'default-news').revision,2)

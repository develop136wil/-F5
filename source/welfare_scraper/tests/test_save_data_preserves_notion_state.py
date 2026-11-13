"""history_manager.save_data()가 기존 항목을 행 교체 없이 안전하게 갱신하는지 검증.

(2026-08-07, GPT Codex 전수조사로 발견) INSERT OR REPLACE 컬럼 목록에 이 두 컬럼이 빠져
있어, 60일 재확인이나 내용 갱신으로 기존 항목이 재저장될 때마다 조용히 NULL/0으로
초기화되고 있었다(실측: 운영 DB 등록 5건·제외 11건 확인). ai_skip_reason 보존과 동일한
패턴으로 수정했다.
"""
import os
import json
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import history_manager
from history_manager import items as item_store


class TestSaveDataPreservesNotionState(unittest.TestCase):
    def setUp(self):
        self.test_db = f'test_save_data_notion_state_{self._testMethodName}.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def test_notion_page_id_preserved_on_resave(self):
        with database.get_connection() as conn:
            conn.execute(
                """INSERT INTO welfare_items (site_name, title, link, scraped_at, notion_page_id)
                   VALUES (?, ?, ?, ?, ?)""",
                ('사이트', '이미등록항목', 'https://x/1', '2026-08-01T00:00:00', 'notion-page-abc'))
            conn.commit()

        # 60일 재확인·내용갱신처럼 notion_page_id를 모르는 상태로 재저장되는 경로 시뮬레이션
        history_manager.save_data([{
            'site_name': '사이트', 'title': '이미등록항목(갱신)', 'link': 'https://x/1',
            'scraped_at': '2026-08-01T00:00:00', 'deadline': '2026-12-31',
        }])

        row = database.execute_query(
            "SELECT notion_page_id FROM welfare_items WHERE link=?",
            ('https://x/1',), fetch_one=True)
        self.assertEqual(row['notion_page_id'], 'notion-page-abc')

    def test_notion_dismissed_preserved_on_resave(self):
        with database.get_connection() as conn:
            conn.execute(
                """INSERT INTO welfare_items (site_name, title, link, scraped_at, notion_dismissed)
                   VALUES (?, ?, ?, ?, ?)""",
                ('사이트', '제외된항목', 'https://x/2', '2026-08-01T00:00:00', 1))
            conn.commit()

        history_manager.save_data([{
            'site_name': '사이트', 'title': '제외된항목(갱신)', 'link': 'https://x/2',
            'scraped_at': '2026-08-01T00:00:00', 'deadline': '2026-12-31',
        }])

        row = database.execute_query(
            "SELECT notion_dismissed FROM welfare_items WHERE link=?",
            ('https://x/2',), fetch_one=True)
        self.assertEqual(row['notion_dismissed'], 1)

    def test_new_item_defaults_stay_unset(self):
        """신규 항목은 당연히 notion 상태가 없어야 한다 — 보존 로직이 엉뚱한 값을 만들면 안 됨."""
        history_manager.save_data([{
            'site_name': '사이트', 'title': '신규항목', 'link': 'https://x/3',
            'scraped_at': '2026-08-01T00:00:00',
        }])
        row = database.execute_query(
            "SELECT notion_page_id, notion_dismissed FROM welfare_items WHERE link=?",
            ('https://x/3',), fetch_one=True)
        self.assertIsNone(row['notion_page_id'])
        self.assertEqual(row['notion_dismissed'], 0)

    def test_row_id_and_snapshot_identity_are_preserved_on_resave(self):
        """같은 링크 갱신은 기존 행을 UPDATE하고 스냅샷도 같은 ID를 가리켜야 한다."""
        with database.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO welfare_items (site_name, title, link, scraped_at)
                   VALUES (?, ?, ?, ?)""",
                ('사이트', '변경 전 제목', 'https://x/id-stable', '2026-08-01T00:00:00'))
            original_id = cursor.lastrowid
            conn.commit()

        history_manager.save_data([{
            'site_name': '사이트', 'title': '변경 후 제목', 'link': 'https://x/id-stable',
            'scraped_at': '2026-09-01T00:00:00',
        }])

        current = database.execute_query(
            "SELECT id, title FROM welfare_items WHERE link=?",
            ('https://x/id-stable',), fetch_one=True)
        snapshot = database.execute_query(
            "SELECT snapshot_data FROM welfare_item_snapshots WHERE item_link=?",
            ('https://x/id-stable',), fetch_one=True)
        self.assertEqual(current['id'], original_id)
        self.assertEqual(current['title'], '변경 후 제목')
        self.assertEqual(json.loads(snapshot['snapshot_data'])['id'], original_id)

    def test_fts_index_updates_in_place_with_preserved_row_id(self):
        """UPSERT의 UPDATE 트리거가 FTS 문서를 같은 rowid로 교체해야 한다."""
        history_manager.save_data([{
            'site_name': '사이트', 'title': '예전고유검색어', 'link': 'https://x/fts-stable',
            'scraped_at': '2026-08-01T00:00:00',
        }])
        before = database.execute_query(
            "SELECT id FROM welfare_items WHERE link=?", ('https://x/fts-stable',), fetch_one=True)

        history_manager.save_data([{
            'site_name': '사이트', 'title': '새로운고유검색어', 'link': 'https://x/fts-stable',
            'scraped_at': '2026-09-01T00:00:00',
        }])

        after = database.execute_query(
            "SELECT id FROM welfare_items WHERE link=?", ('https://x/fts-stable',), fetch_one=True)
        old_hits = database.execute_query(
            "SELECT rowid FROM welfare_items_fts WHERE welfare_items_fts MATCH ?", ('예전고유검색어',))
        new_hits = database.execute_query(
            "SELECT rowid FROM welfare_items_fts WHERE welfare_items_fts MATCH ?", ('새로운고유검색어',))
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(old_hits, [])
        self.assertEqual([row['rowid'] for row in new_hits], [before['id']])

    def test_result_reports_actual_saved_and_duplicate_skipped_items(self):
        now = datetime.now().isoformat()
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '이미 있는 제목', 'https://x/original', now))
            conn.commit()

        result = history_manager.save_data([
            {'site_name': '사이트', 'title': '새 항목', 'link': 'https://x/new', 'scraped_at': now},
            {'site_name': '사이트', 'title': '이미 있는 제목', 'link': 'https://x/duplicate',
             'scraped_at': now},
        ])

        self.assertEqual(result['requested'], 2)
        self.assertEqual(result['saved'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['saved_links'], ['https://x/new'])
        self.assertEqual(result['skipped_links'], ['https://x/duplicate'])

    def test_item_failure_rolls_back_whole_batch_and_is_raised(self):
        with self.assertRaises(Exception):
            history_manager.save_data([
                {'site_name': '사이트', 'title': '정상', 'link': 'https://x/rollback-ok'},
                {'site_name': '사이트', 'title': None, 'link': 'https://x/rollback-fail'},
            ])
        rows = database.execute_query(
            "SELECT link FROM welfare_items WHERE link LIKE 'https://x/rollback-%'")
        self.assertEqual(rows, [])

    def test_snapshot_failure_rolls_back_update_and_is_raised(self):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link) VALUES (?,?,?)",
                ('사이트', '원래 제목', 'https://x/snapshot-fail'))
            conn.commit()

        with patch.object(item_store, '_save_snapshot', side_effect=OSError('snapshot disk full')):
            with self.assertRaisesRegex(OSError, 'snapshot disk full'):
                history_manager.save_data([{
                    'site_name': '사이트', 'title': '바뀐 제목', 'link': 'https://x/snapshot-fail',
                }])

        row = database.execute_query(
            "SELECT title FROM welfare_items WHERE link=?",
            ('https://x/snapshot-fail',), fetch_one=True)
        self.assertEqual(row['title'], '원래 제목')


if __name__ == '__main__':
    unittest.main()

"""전수조사에서 발견한 파괴적 오류의 회귀 테스트. 실제 외부 API는 호출하지 않는다."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from history_manager import items, maintenance
from services import policy_sync, notion_service


class TestDeadlineSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.close_all_connections()
        self.db_patch = patch.object(database, 'DB_FILE', str(Path(self.tmp.name) / 'test.db'))
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patch.stop()
        self.tmp.cleanup()

    def insert(self, title, deadline, age=0):
        database.execute_write(
            'INSERT INTO welfare_items (title, link, deadline, scraped_at) VALUES (?, ?, ?, ?)',
            (title, 'https://example.invalid/' + title, deadline,
             (datetime.now() - timedelta(days=age)).isoformat()))

    def test_legacy_unknown_and_invalid_deadlines_are_not_expired(self):
        values = [None, '', ' ', '상시', '미정', '0', '2025-02-30', '2025-13-01', '2025-1-1']
        for i, value in enumerate(values):
            self.insert(str(i), value)
        self.assertEqual(maintenance.archive_expired_items(), 0)
        self.assertEqual(maintenance.cleanup_data(365), 0)
        self.assertEqual(len(database.execute_query('SELECT id FROM welfare_items')), len(values))

    def test_valid_expiry_and_retention_still_work(self):
        self.insert('expired', (datetime.now() - timedelta(days=40)).strftime('%Y-%m-%d'))
        self.insert('recent-expiry', (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'))
        self.insert('future', (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d'))
        self.insert('old-unknown', None, age=400)
        self.assertEqual(maintenance.archive_expired_items(), 2)
        self.assertEqual(maintenance.cleanup_data(365), 2)
        remaining = {row['title'] for row in database.execute_query('SELECT title FROM welfare_items')}
        self.assertEqual(remaining, {'recent-expiry', 'future'})

    def test_new_blank_deadline_is_stored_as_null(self):
        items.save_data([{'title': 'blank', 'link': 'https://example.invalid/blank',
                          'site_name': 'test', 'deadline': '   '}])
        row = database.execute_query('SELECT deadline FROM welfare_items', fetch_one=True)
        self.assertIsNone(row['deadline'])


class TestNotionReadSafety(unittest.IsolatedAsyncioTestCase):
    async def test_punctuation_and_numeric_edits_are_stale_even_when_forced(self):
        for previous, live in [('2.5만원', '25만원'), ('5-9세', '59세'),
                               ('부모/자녀', '부모자녀'), ('A|B', 'AB'), ('1 000원', '1000원')]:
            for force in (False, True):
                with self.subTest(previous=previous, live=live, force=force):
                    target = {'page_id': 'fake', 'fkey': 'target_income', 'old_display': previous,
                              'py_value': '새 제안', 'properties': {'대상 소득 수준': {}}}
                    read_db = AsyncMock(return_value=(target, None, {}))
                    update = AsyncMock()
                    with patch.object(database, 'run_db', read_db), \
                         patch.object(notion_service, 'get_page', AsyncMock(return_value={'id': 'fake'})), \
                         patch.object(notion_service, 'extract_page_fields', return_value={'target_income': live}), \
                         patch.object(notion_service, 'update_page_properties', update):
                        result = await policy_sync._apply_field_to_notion_locked(1, '대상 소득 수준', force, force)
                    self.assertEqual(result['error'], 'STALE')
                    update.assert_not_awaited()
                    self.assertEqual(read_db.await_count, 1)

    def test_write_precondition_only_normalizes_whitespace_and_tag_display(self):
        compare = policy_sync._live_field_matches_snapshot
        self.assertTrue(compare('target_income', '  월\n  2.5만원  ', '월 2.5만원'))
        self.assertTrue(compare('life_cycle', ['청소년', '영유아'], ' 영유아,  청소년 '))
        self.assertTrue(compare('residence', ['서울특별시 도봉구'], '서울특별시  도봉구'))
        self.assertTrue(compare('target_characteristics', [], ''))
        self.assertFalse(compare('life_cycle', ['청소년'], '영유아, 청소년'))
        self.assertFalse(compare('target_characteristics', ['장애발달지연'], '장애/발달지연'))
        self.assertFalse(compare('life_cycle', None, ''))
        self.assertFalse(compare('start_age_months', 0, ''))

    async def test_failed_read_never_patches_or_finalizes_even_when_forced(self):
        target = {'page_id': 'fake', 'fkey': 'contact', 'old_display': 'old',
                  'py_value': 'new', 'properties': {'문의처': {}}}
        for force in (False, True):
            with self.subTest(force=force):
                run_db = AsyncMock(side_effect=[(target, None, {}), 0])
                update = AsyncMock(return_value={'ok': True})
                with patch.object(database, 'run_db', run_db), \
                     patch.object(notion_service, 'get_page', AsyncMock(return_value=None)), \
                     patch.object(notion_service, 'update_page_properties', update):
                    result = await policy_sync._apply_field_to_notion_locked(1, '문의처', force, force)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'LIVE_READ_FAILED')
                update.assert_not_awaited()
                self.assertEqual(run_db.await_count, 1)

    async def test_changed_live_value_still_returns_stale(self):
        target = {'page_id': 'fake', 'fkey': 'contact', 'old_display': 'old',
                  'py_value': 'new', 'properties': {'문의처': {}}}
        update = AsyncMock()
        with patch.object(database, 'run_db', AsyncMock(return_value=(target, None, {}))), \
             patch.object(notion_service, 'get_page', AsyncMock(return_value={'id': 'fake'})), \
             patch.object(notion_service, 'extract_page_fields', return_value={'contact': 'edited'}), \
             patch.object(notion_service, 'update_page_properties', update):
            result = await policy_sync._apply_field_to_notion_locked(1, '문의처', False, False)
        self.assertEqual(result['error'], 'STALE')
        update.assert_not_awaited()

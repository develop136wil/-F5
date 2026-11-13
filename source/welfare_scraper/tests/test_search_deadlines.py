"""Offline Korean midnight and legacy unknown-deadline regression coverage."""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np
import database
from history_manager import items, embeddings
from services import embedding_service, search_dates


class SearchDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY, site_name TEXT, title TEXT, link TEXT,
            date TEXT, category TEXT, target TEXT, residence TEXT, deadline TEXT,
            event_date TEXT, event_end_date TEXT, scraped_at TEXT,
            support_detail TEXT, embedding BLOB, embedding_recipe TEXT,
            is_archived INTEGER DEFAULT 0)''')
        self.conn.execute('CREATE VIRTUAL TABLE welfare_items_fts USING fts5(title)')
        for ident, deadline in enumerate(('2026-09-08', '2026-09-09', '2026-09-10', None, '', '  '), 1):
            self.conn.execute('''INSERT INTO welfare_items
                (id,title,link,deadline,category,embedding,embedding_recipe,scraped_at)
                VALUES (?,?,?,?,?,?,?,?)''', (ident, '지원 안내', f'https://example.invalid/{ident}',
                deadline, '복지', embedding_service.to_blob(np.array([1., 0., 0.], dtype=np.float32)),
                embedding_service.SEARCH_EMBEDDING_RECIPE, '2026-09-09'))
            self.conn.execute('INSERT INTO welfare_items_fts(rowid,title) VALUES (?,?)', (ident, '지원 안내'))
        self.conn.commit()

        @contextmanager
        def connection():
            yield self.conn
        self.db_patch = patch.object(database, 'get_connection', connection)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def ids(self, result):
        return {row['id'] for row in result['items']}

    def test_korean_midnight_does_not_depend_on_machine_local_timezone(self):
        cases = ((datetime(2026, 9, 8, 14, 59, tzinfo=timezone.utc), '2026-09-08'),
                 (datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc), '2026-09-09'),
                 (datetime(2026, 9, 8, 23, 59, tzinfo=timezone.utc), '2026-09-09'))
        for moment, expected in cases:
            self.assertEqual(search_dates.today_kst(moment), expected)
        with self.assertRaises(ValueError):
            search_dates.today_kst(datetime(2026, 9, 9))

    def test_keyword_semantic_and_hybrid_exclude_yesterday_at_korean_midnight(self):
        moment = datetime(2026, 9, 8, 15, 30, tzinfo=timezone.utc)
        with patch.object(search_dates, 'today_kst', return_value=search_dates.today_kst(moment)):
            self.assertEqual(self.ids(items.search_items('지원', active_only=True)), {2, 3, 4, 5, 6})
            self.assertEqual(self.ids(embeddings.semantic_search([1., 0., 0.], active_only=True)), {2, 3, 4, 5, 6})
            self.assertEqual(self.ids(embeddings.hybrid_search('지원', [1., 0., 0.], active_only=True)), {2, 3, 4, 5, 6})
            self.assertEqual(self.ids(embeddings.hybrid_search('지원', None, active_only=True)), {2, 3, 4, 5, 6})

    def test_unknown_deadlines_are_active_not_closed_and_today_stays_active(self):
        with patch.object(search_dates, 'today_kst', return_value='2026-09-09'):
            self.assertEqual(self.ids(items.search_items(status='active')), {2, 3, 4, 5, 6})
            self.assertEqual(self.ids(items.search_items(status='closed')), {1})
            self.assertEqual(self.ids(items.search_items()), {1, 2, 3, 4, 5, 6})

    def test_hybrid_uses_one_day_even_when_request_spans_midnight(self):
        with patch.object(search_dates, 'today_kst', side_effect=['2026-09-08', '2026-09-09']) as clock:
            result = embeddings.hybrid_search('지원 안내', [1., 0., 0.], active_only=True)
        self.assertEqual(self.ids(result), {1, 2, 3, 4, 5, 6})
        clock.assert_called_once()

    def test_category_and_archive_filters_survive_both_search_branches(self):
        self.conn.execute('UPDATE welfare_items SET is_archived=1 WHERE id=2')
        self.conn.execute("UPDATE welfare_items SET category='기타' WHERE id=3")
        with patch.object(search_dates, 'today_kst', return_value='2026-09-09'):
            self.assertEqual(self.ids(items.search_items('지원', category='복지', active_only=True)), {4, 5, 6})
            self.assertEqual(self.ids(embeddings.hybrid_search('지원', [1., 0., 0.], category='복지', active_only=True)), {4, 5, 6})
            self.assertEqual(self.ids(embeddings.hybrid_search('지원', [1., 0., 0.], category='복지', active_only=True, include_archived=True)), {2, 4, 5, 6})

    def test_internal_snapshot_date_is_validated_before_sql(self):
        for value in ('20260909', '2026-09-09 OR 1=1', '2026-13-01', 1):
            with self.assertRaises((TypeError, ValueError)):
                search_dates.deadline_filter(active=True, as_of_date=value)

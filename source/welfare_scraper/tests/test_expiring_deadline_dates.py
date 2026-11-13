"""Read-only expiry windows use Korean dates and keep the final day's clocks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

import database
from history_manager import stats
from services import search_dates


class TestExpiringDeadlineDates(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY, title TEXT, deadline TEXT,
            is_archived INTEGER, ai_data TEXT, embedding BLOB,
            category TEXT, site_name TEXT, scraped_at TEXT)''')
        self.conn.execute('CREATE TABLE scrape_runs (status TEXT)')

        @contextmanager
        def connection():
            yield self.conn
        self.db_patch = patch.object(database, 'get_connection', connection)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def add(self, name, deadline, archived=0):
        self.conn.execute('''INSERT INTO welfare_items
            (title,deadline,is_archived,ai_data,embedding) VALUES (?,?,?,?,?)''',
            (name, deadline, archived, '{"source":"test"}', b'vector'))
        self.conn.commit()

    def test_seventh_day_includes_date_space_and_iso_local_clocks(self):
        for name, value in (
                ('yesterday', '2026-09-09 23:59'), ('today', '2026-09-10'),
                ('today-clock', '2026-09-10T00:00'), ('last-date', '2026-09-17'),
                ('last-space', '2026-09-17 18:00'), ('last-iso', '2026-09-17T23:59'),
                ('tomorrow', '2026-09-18 00:00')):
            self.add(name, value)
        self.add('archived', '2026-09-17 18:00', archived=1)
        self.add('null-archive', '2026-09-17 18:00', archived=None)
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM welfare_items')]
        changes = self.conn.total_changes
        with patch.object(search_dates, 'today_kst', return_value='2026-09-10') as clock:
            result = stats.get_expiring_items(7)
        clock.assert_called_once()
        self.assertEqual({row['title'] for row in result},
                         {'today', 'today-clock', 'last-date', 'last-space',
                          'last-iso', 'null-archive'})
        self.assertEqual(next(row['deadline'] for row in result if row['title'] == 'last-space'),
                         '2026-09-17 18:00')
        self.assertTrue(all('embedding' not in row and row['ai_data'] == {'source': 'test'} for row in result))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM welfare_items')], before)

    def test_korean_midnight_not_sqlite_utc_or_machine_local_day(self):
        self.add('yesterday', '2026-09-09 23:59')
        self.add('today', '2026-09-10 00:00')
        self.add('tomorrow', '2026-09-11')
        with patch.object(search_dates, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)
            result = stats.get_expiring_items(0)
        self.assertEqual([row['title'] for row in result], ['today'])
        clock.now.assert_called_once_with(timezone.utc)

    def test_unknown_malformed_and_unconfirmed_timezones_do_not_get_counted(self):
        for index, value in enumerate((None, '', ' ', '상시', '2026-09-17 미정',
                                      '2026-09-17 24:00', '2026-09-17 18:60',
                                      '2026-09-17 18:00:99', '2026-9-17',
                                      '2026-09-17 18:00:00',
                                      '2026-09-17T18:00+09:00', '2026-09-17T09:00Z')):
            self.add(str(index), value)
        with patch.object(search_dates, 'today_kst', return_value='2026-09-10'):
            self.assertEqual(stats.get_expiring_items(), [])

    def test_invalid_calendar_day_is_not_a_deadline(self):
        self.add('invalid', '2026-02-30 18:00')
        self.add('valid', '2026-03-01 18:00')
        with patch.object(search_dates, 'today_kst', return_value='2026-02-28'):
            self.assertEqual([row['title'] for row in stats.get_expiring_items()], ['valid'])

    def test_summary_count_matches_open_list_on_korean_date_boundary(self):
        for name, value in (('yesterday', '2026-09-09 23:59'),
                            ('today', '2026-09-10'), ('last', '2026-09-17T23:59'),
                            ('invalid', '2026-09-17 24:00'),
                            ('after', '2026-09-18'), ('unknown', None)):
            self.add(name, value)
        self.add('archived', '2026-09-17 18:00', archived=1)
        before = self.conn.total_changes
        with patch.object(search_dates, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)
            summary = stats.get_summary_stats()
            result = stats.get_expiring_items()
        self.assertEqual(summary['expiring_soon'], len(result))
        self.assertEqual(summary['expiring_soon'], 2)
        self.assertEqual(self.conn.total_changes, before)

    def test_existing_long_window_stays_supported_but_invalid_days_reject(self):
        self.add('future', '2099-01-01')
        with patch.object(search_dates, 'today_kst', return_value='2026-09-10'):
            self.assertEqual([row['title'] for row in stats.get_expiring_items(365 * 100)], ['future'])
        for value in (-1, True, '7', None, 0.5):
            with self.subTest(days=value), self.assertRaises(ValueError):
                stats.get_expiring_items(value)

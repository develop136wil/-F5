"""DB failure must be distinguishable from a legitimate empty result."""
import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import api_server
import database
import utils
from history_manager import embeddings, health, items, maintenance, snapshots, stats


class DatabaseFailurePropagationTests(unittest.TestCase):
    def test_reads_and_mutations_propagate_database_and_maintenance_errors(self):
        calls = [
            (utils.load_sites, ()), (utils.delete_site, (1,)),
            (items.load_history, ()), (items.load_data, ()),
            (items.search_items, ()), (items.get_recent_items, ()),
            (items.get_items_by_links, (['https://example.invalid/item'],)),
            (items.get_item_by_id, (1,)), (items.count_ai_backlog, ()),
            (items.count_ai_permanently_excluded, ()), (items.get_ai_backlog, (10,)),
            (stats.get_summary_stats, ()), (stats.get_trend_stats, ()),
            (stats.get_keyword_suggestions, ()), (stats.get_expiring_items, ()),
            (health.get_sites_last_scraped, (['synthetic'],)),
            (health.get_health_stats, ()), (health.get_site_health_dashboard, ()),
            (health.get_site_scrape_history, (1,)),
            (snapshots.get_recently_changed_items, ()),
            (snapshots.get_item_snapshots, ('https://example.invalid/item',)),
            (snapshots.get_item_snapshots, (None, 1)),
            (snapshots.get_item_diff, (1, 1)),
            (embeddings.find_cached_ai_data_by_hash, ('synthetic',)),
            (embeddings.get_items_missing_embedding, ()), (embeddings.count_embeddings, ()),
            (embeddings.get_embedding_index, ()), (embeddings.get_daily_ai_count, ()),
            (embeddings.semantic_search, ([1.0, 0.0],)),
            (embeddings.get_similar_items, (1,)),
            (maintenance.cleanup_data, (90,)), (maintenance.archive_expired_items, ()),
        ]
        for error in (sqlite3.OperationalError('synthetic failure'),
                      database.DatabaseMaintenanceError('synthetic maintenance')):
            for function, args in calls:
                with self.subTest(function=function.__name__, error=type(error).__name__):
                    with patch.object(database, 'get_connection', side_effect=error):
                        with self.assertRaises(type(error)) as raised:
                            function(*args)
                    self.assertIs(raised.exception, error)

    def test_cancelled_read_does_not_turn_into_empty_success(self):
        with patch.object(database, 'get_connection', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                items.load_data()

    def test_automatic_cleanup_failure_does_not_record_success_date(self):
        with patch.object(maintenance, 'archive_expired_items', return_value=0), \
             patch.object(maintenance, 'cleanup_data', side_effect=sqlite3.OperationalError('synthetic failure')), \
             patch.object(utils, 'get_env_value', return_value=''), \
             patch.object(utils, 'set_env_value') as save:
            maintenance.run_maintenance(retention_days=365)
        save.assert_not_called()


class IsolatedDatabaseApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.patches = [patch.object(database, 'DB_FILE', os.path.join(self.temporary.name, 'api.db')),
                        patch.object(database, '_local', threading.local()),
                        patch.object(database, '_all_conns', set()),
                        patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN': ''})]
        for item in self.patches:
            item.start()
        database.init_db()

    def tearDown(self):
        database.close_all_connections(strict=True)
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def test_empty_database_has_normal_successful_results(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url='http://test') as client:
            for path in ('/api/sites', '/api/data', '/api/data/recent', '/api/reports/trends'):
                with self.subTest(path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), [])
            summary = await client.get('/api/reports/summary')
            self.assertEqual(summary.status_code, 200)
            self.assertEqual(summary.json()['total_items'], 0)
            for path, key in (('/api/data/cleanup', 'deleted_count'),
                              ('/api/data/archive-expired', 'archived_count')):
                response = await client.post(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()[key], 0)

    async def test_database_failure_is_safe_503_instead_of_successful_empty(self):
        import httpx
        sentinel = 'private-fixture-path-and-content'
        calls = [('GET', path) for path in ('/api/sites', '/api/data', '/api/data/recent',
                 '/api/reports/summary', '/api/reports/trends', '/api/sites/health',
                 '/api/health', '/api/embeddings/status', '/api/ai-backlog/status')]
        calls += [('POST', '/api/data/cleanup'), ('POST', '/api/data/archive-expired'),
                  ('DELETE', '/api/sites/1')]
        with patch.object(database, 'get_connection', side_effect=sqlite3.OperationalError(sentinel)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_server.app), base_url='http://test') as client:
                for method, path in calls:
                    with self.subTest(method=method, path=path):
                        response = await client.request(method, path)
                        self.assertEqual(response.status_code, 503)
                        self.assertIn('detail', response.json())
                        self.assertNotIn(sentinel, response.text)

    async def test_cleanup_mid_transaction_failure_rolls_back_deleted_items(self):
        with database.get_connection() as connection:
            connection.execute("INSERT INTO welfare_items(title,link,scraped_at) VALUES ('old','https://example.invalid/old','2000-01-01')")
            connection.execute("INSERT INTO welfare_item_snapshots(item_link,snapshot_data,created_at) VALUES ('https://example.invalid/old','{}','2000-01-01')")
            connection.execute("CREATE TRIGGER deny_cleanup BEFORE DELETE ON welfare_item_snapshots BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            maintenance.cleanup_data(90)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM welfare_item_snapshots').fetchone()[0], 1)

"""Bounded backfill traversal with in-memory SQLite and no real AI requests."""
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch


class EmbeddingBackfillCursor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import api_server
        import history_manager
        from services import embedding_service
        self.api, self.history, self.embedding = api_server, history_manager, embedding_service
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY, link TEXT, title TEXT, support_detail TEXT,
            category TEXT, target TEXT, residence TEXT, target_characteristics TEXT,
            life_cycle TEXT, age_detail TEXT, target_income TEXT, eligibility_detail TEXT,
            scraped_at TEXT, embedding BLOB, embedding_recipe TEXT)''')

        @contextmanager
        def connection():
            yield self.conn

        async def call_db(function, *args, **kwargs):
            return function(*args, **kwargs)

        async def vectorize(texts, **kwargs):
            return [[1.0, 0.0] for _ in texts]

        self.embed = AsyncMock(side_effect=vectorize)
        self.db = AsyncMock(side_effect=call_db)
        for patcher in (
            patch.object(api_server.database, 'get_connection', connection),
            patch.object(api_server.database, 'run_db', self.db),
            patch.object(embedding_service, 'get_unavailable_reason', return_value=None),
            patch.object(embedding_service, 'embed_texts_batch_async', self.embed),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.close)

    def insert(self, identifier, *, long=False, embedded=False):
        self.conn.execute('''INSERT INTO welfare_items
            (id,link,title,support_detail,scraped_at,embedding,embedding_recipe)
            VALUES (?,?,?,?,?,?,?)''',
            (identifier, f'https://example.invalid/{identifier}', f'자료 {identifier}',
             '가' * 1000 if long else '지원 내용', '2026-09-10',
             b'already-stored' if embedded else None,
             self.embedding.SEARCH_EMBEDDING_RECIPE if embedded else None))
        self.conn.commit()

    async def test_earliest_long_row_does_not_consume_candidate_limit(self):
        self.insert(1, long=True)
        self.insert(2)
        result = await self.api._embeddings_backfill_locked(1)
        self.assertEqual(result['embedded'], 1)
        self.assertEqual(result['held_for_input_size'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['remaining'], 1)
        self.assertIsNone(result['next_cursor'])
        self.assertTrue(result['scan_complete'])
        self.embed.assert_awaited_once()
        self.assertIn('자료 2', self.embed.call_args.args[0][0])
        self.assertEqual(self.conn.execute('SELECT support_detail FROM welfare_items WHERE id=1').fetchone()[0], '가' * 1000)
        self.assertIsNone(self.conn.execute('SELECT embedding FROM welfare_items WHERE id=1').fetchone()[0])

    async def test_more_than_one_full_held_window_reaches_later_valid_input(self):
        for identifier in range(1, 206):
            self.insert(identifier, long=True)
        self.insert(206)
        cursor, pages, held = 0, [], 0
        for _ in range(4):
            result = await self.api._embeddings_backfill_locked(1, cursor=cursor)
            pages.append(result)
            held += result['held_for_input_size']
            self.assertLessEqual(result['scanned'], 100)
            if result['scan_complete']:
                break
            self.assertGreater(result['next_cursor'], cursor)
            cursor = result['next_cursor']
        self.assertEqual([page['next_cursor'] for page in pages], [100, 200, None])
        self.assertEqual(held, 205)
        self.assertEqual(sum(page['embedded'] for page in pages), 1)
        self.embed.assert_awaited_once()
        queries = [call for call in self.db.call_args_list if call.args[0] is self.history.get_items_missing_embedding]
        self.assertEqual([call.args[1] for call in queries], [101, 101, 101])
        self.assertEqual([call.kwargs['after_id'] for call in queries], [0, 100, 200])

    async def test_all_held_scan_terminates_without_any_ai_call(self):
        for identifier in range(1, 251):
            self.insert(identifier, long=True)
        cursor, held, iterations = 0, 0, 0
        while iterations < 4:
            iterations += 1
            result = await self.api._embeddings_backfill_locked(1, cursor=cursor)
            held += result['held_for_input_size']
            if result['scan_complete']:
                break
            self.assertGreater(result['next_cursor'], cursor)
            cursor = result['next_cursor']
        self.assertEqual(iterations, 3)
        self.assertEqual(held, 250)
        self.assertEqual(result['remaining'], 250)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(result['scan_complete'])
        self.embed.assert_not_awaited()

    async def test_failure_cursor_cannot_advance_past_first_unfinished_input(self):
        self.insert(1, long=True)
        self.insert(2)
        self.insert(3)
        self.embed.side_effect = None
        self.embed.return_value = [None, [1.0, 0.0]]
        result = await self.api._embeddings_backfill_locked(2)
        self.assertEqual(result['embedded'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['next_cursor'], 1)
        self.assertFalse(result['scan_complete'])
        self.embed.return_value = [[1.0, 0.0]]
        resumed = await self.api._embeddings_backfill_locked(2, cursor=result['next_cursor'])
        self.assertEqual(resumed['embedded'], 1)
        self.assertTrue(resumed['scan_complete'])
        self.assertEqual(len(self.embed.call_args.args[0]), 1)
        self.assertIn('자료 2', self.embed.call_args.args[0][0])

    async def test_candidate_cap_keeps_unconsumed_rows_for_next_page(self):
        for identifier in range(1, 6):
            self.insert(identifier)
        first = await self.api._embeddings_backfill_locked(2)
        self.assertEqual(first['embedded'], 2)
        self.assertEqual(first['scanned'], 2)
        self.assertEqual(first['next_cursor'], 2)
        second = await self.api._embeddings_backfill_locked(2, cursor=2)
        self.assertEqual(second['embedded'], 2)
        self.assertEqual(second['next_cursor'], 4)
        last = await self.api._embeddings_backfill_locked(2, cursor=4)
        self.assertEqual(last['embedded'], 1)
        self.assertIsNone(last['next_cursor'])

    async def test_wrong_provider_response_count_is_failed_not_silent_completion(self):
        self.insert(1)
        self.insert(2)
        self.embed.side_effect = None
        self.embed.return_value = [[1.0, 0.0]]
        result = await self.api._embeddings_backfill_locked(2)
        self.assertEqual(result['embedded'], 0)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(result['next_cursor'], 0)
        self.assertFalse(result['scan_complete'])

    async def test_database_save_failure_is_retryable_at_same_cursor(self):
        self.insert(1)
        with patch.object(self.history, 'save_embedding', side_effect=sqlite3.OperationalError('synthetic disk full')):
            result = await self.api._embeddings_backfill_locked(1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['next_cursor'], 0)
        self.assertFalse(result['scan_complete'])

    def test_repository_keyset_is_bounded_and_excludes_already_saved_rows(self):
        for identifier in range(1, 6):
            self.insert(identifier, embedded=identifier == 3)
        rows = self.history.get_items_missing_embedding(2, after_id=2)
        self.assertEqual([row['id'] for row in rows], [4, 5])
        for value in (-1, True, '0', 9223372036854775808):
            with self.assertRaises(ValueError):
                self.history.get_items_missing_embedding(2, after_id=value)
        for value in (-1, 0, 1002, True):
            with self.assertRaises(ValueError):
                self.history.get_items_missing_embedding(value, after_id=0)

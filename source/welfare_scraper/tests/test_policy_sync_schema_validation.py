"""First-use schema checks: isolated SQLite and fake Notion; no provider calls."""
import copy
import json
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import database
from services import notion_service as ns, policy_full_check as flow, policy_sync as ps
from tests import test_check_policies_baseline_failure as fixtures


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _schema(**problems):
    return {'ok': True, 'schema': {'category': ['기관별 분류'], 'residence': []},
            'missing_properties': [], 'type_mismatches': [], **problems}


class PolicySyncSchemaValidation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        self.saved_statuses = [(status, copy.deepcopy(status)) for status in (
            ps.SYNC_STATUS, ps.POLICY_STATUS, ps.AUDIT_STATUS,
            ps.RECOMPUTE_STATUS, ps.STYLE_STATUS, ps.ONE_CLICK_STATUS)]
        database.execute_write(
            'INSERT INTO policy_db_schema (db_key,schema_json) VALUES (?,?)',
            ('medical', json.dumps({'category': ['기존 선택지']})))
        database.execute_write(
            "UPDATE notion_policies SET pending_diff=?,change_status='detected',audited_at=?",
            (json.dumps({'fields': [{'field': '상세 지원 내용', 'new': '보존할 제안'}]}), 'saved-audit'))

    def tearDown(self):
        for status, saved in self.saved_statuses:
            status.clear()
            status.update(saved)
        fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown(self)

    def snapshot(self):
        return {
            'policies': [dict(row) for row in database.execute_query(
                'SELECT * FROM notion_policies ORDER BY id')],
            'schema': dict(database.execute_query(
                'SELECT * FROM policy_db_schema WHERE db_key=?', ('medical',), fetch_one=True)),
        }

    @contextmanager
    def mocked_notion(self, schemas, *, keys=None):
        configured = [{'key': key, 'name': key, 'data_source_id': key}
                      for key in (keys or ['medical'])]

        async def target(_session, _token, key, *_args):
            return {'ok': True, 'kind': 'data_source', 'id': key}

        async def schema(_session, _token, destination, *_args):
            return schemas[destination['id']]

        with patch.object(ns, 'get_credentials', return_value=('synthetic', '')), \
             patch.object(ns, 'get_policy_dbs', return_value=configured), \
             patch.object(ns.aiohttp, 'ClientSession', return_value=_Session()), \
             patch.object(ns, 'resolve_target', new=AsyncMock(side_effect=target)), \
             patch.object(ns, 'get_policy_source_schema', new=AsyncMock(side_effect=schema)), \
             patch.object(ns, 'query_policy_source_pages', new=AsyncMock(
                 return_value={'ok': True, 'pages': []})) as pages, \
             patch.object(ps, '_embed_and_upsert_batch', new=AsyncMock(
                 wraps=ps._embed_and_upsert_batch)) as upsert, \
             patch.object(ps, '_db_write_strict', new=AsyncMock(wraps=ps._db_write_strict)) as writes, \
             patch.object(ps.embedding_service, 'embed_texts_batch_async', new=AsyncMock(
                 side_effect=AssertionError('No AI calls in schema validation'))) as embedding:
            yield pages, upsert, writes, embedding

    async def assert_invalid_preserves_mirror(self, schema_result, expected):
        before = self.snapshot()
        with self.mocked_notion({'medical': schema_result}) as (pages, upsert, writes, embedding):
            result = await ps.sync_policies(['medical'], defer_embeddings=True)
        self.assertFalse(result['ok'])
        self.assertEqual((result['synced'], result['removed'], result['dbs']), (0, 0, []))
        self.assertIn(expected, result['error'])
        self.assertIn('설정의 Notion 연결 확인', result['error'])
        pages.assert_not_awaited()
        upsert.assert_not_awaited()
        writes.assert_not_awaited()
        embedding.assert_not_awaited()
        self.assertEqual(self.snapshot(), before)
        status = database.execute_query(
            'SELECT last_error FROM policy_db_status WHERE db_key=?', ('medical',), fetch_one=True)
        self.assertIn(expected, status['last_error'])

    async def test_missing_property_stops_before_pages_cache_and_embedding(self):
        await self.assert_invalid_preserves_mirror(
            _schema(missing_properties=['사업명', '관련 홈페이지 1']), '사업명')

    async def test_wrong_property_type_stops_before_pages_cache_and_embedding(self):
        await self.assert_invalid_preserves_mirror(_schema(type_mismatches=[
            {'name': '분류', 'expected': 'select', 'actual': 'rich_text'}]), '분류')

    async def test_invalid_empty_table_is_failure_not_zero_item_success(self):
        database.execute_write('DELETE FROM notion_policies')
        await self.assert_invalid_preserves_mirror(
            _schema(missing_properties=['사업명']), '사업명')

    async def test_valid_empty_template_is_accepted_without_existing_rows_or_ai(self):
        database.execute_write('DELETE FROM notion_policies')
        database.execute_write('DELETE FROM policy_db_schema')
        # Empty select options are valid too; required columns are a separate check.
        valid = _schema(schema={'category': [], 'residence': [],
                                'target_characteristics': [], 'life_cycle': []})
        with self.mocked_notion({'medical': valid}) as (pages, upsert, _writes, embedding):
            result = await ps.sync_policies(['medical'], defer_embeddings=True)
        self.assertTrue(result['ok'])
        self.assertEqual((result['synced'], result['removed'], result['dbs']), (0, 0, ['medical']))
        self.assertEqual(result['failures'], [])
        pages.assert_awaited_once()
        upsert.assert_awaited_once()
        embedding.assert_not_awaited()
        cached = database.execute_query('SELECT schema_json FROM policy_db_schema', fetch_one=True)
        self.assertEqual(json.loads(cached['schema_json']), valid['schema'])
        status = database.execute_query('SELECT last_sync_count,last_error FROM policy_db_status', fetch_one=True)
        self.assertEqual(status['last_sync_count'], 0)
        self.assertIsNone(status['last_error'])

    async def test_one_bad_table_preserves_all_mirrors_and_keeps_partial_success(self):
        database.execute_write(
            'INSERT INTO notion_policies (db_key,page_id,title,notion_fields,source_urls) VALUES (?,?,?,?,?)',
            ('valid', 'previous-valid-page', '유지할 정책', '{}', '[]'))
        before = self.snapshot()
        schemas = {'valid': _schema(), 'medical': _schema(missing_properties=['사업명'])}
        with self.mocked_notion(schemas, keys=['valid', 'medical']) as (pages, _upsert, writes, embedding):
            result = await ps.sync_policies([], defer_embeddings=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['dbs'], ['valid'])
        self.assertEqual([item['key'] for item in result['failures']], ['medical'])
        self.assertEqual(result['removed'], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(pages.await_count, 1)
        self.assertEqual(pages.await_args.args[2]['id'], 'valid')
        self.assertFalse(any('DELETE FROM notion_policies' in str(call) for call in writes.await_args_list))
        embedding.assert_not_awaited()

    async def test_main_check_surfaces_schema_error_and_never_starts_later_stages(self):
        before = self.snapshot()
        with self.mocked_notion({'medical': _schema(missing_properties=['사업명'])}), \
             patch.object(ps, 'recompute_diffs', new=AsyncMock()) as recompute, \
             patch.object(ps, 'check_policies', new=AsyncMock()) as check, \
             patch.object(ps, 'restyle_pending_diffs', new=AsyncMock()) as style, \
             patch.object(flow, 'fill_missing_embeddings', new=AsyncMock()) as embedding:
            result = await ps.run_full_check(['medical'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'error')
        self.assertIn('사업명', result['error'])
        self.assertEqual(ps.ONE_CLICK_STATUS['state'], 'error')
        self.assertIn('사업명', ps.ONE_CLICK_STATUS['message'])
        self.assertIn('설정의 Notion 연결 확인', ps.ONE_CLICK_STATUS['message'])
        self.assertNotIn('점검 완료', ps.ONE_CLICK_STATUS['message'])
        self.assertFalse(ps.ONE_CLICK_STATUS['running'])
        self.assertFalse(ps.SYNC_STATUS['running'])
        self.assertEqual(self.snapshot(), before)
        for action in (recompute, check, style, embedding):
            action.assert_not_awaited()

    async def test_main_error_detail_is_redacted_and_bounded_before_display(self):
        error = '속성 확인 필요\n token=synthetic-sensitive-value ' + '안내 ' * 400
        with patch.object(ps, 'sync_policies', new=AsyncMock(return_value={'ok': False, 'error': error})):
            await ps.run_full_check(['medical'])
        message = ps.ONE_CLICK_STATUS['message']
        self.assertIn('속성 확인 필요', message)
        self.assertIn('[REDACTED]', message)
        self.assertNotIn('synthetic-sensitive-value', message)
        self.assertNotIn('\n', message)
        self.assertLess(len(message), 700)

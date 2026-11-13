"""Offline first-use journeys: empty rows versus explicitly empty option schema.

All Notion/AI responses are synthetic; no live credentials or network calls.
"""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
import api_server as api
import database
from services import ai_verified_input, notion_service as ns, policy_sync as ps
from services import policy_full_check as flow
from services.policy_source_units import source_units
from tests.test_policy_final_connection_audit import live_page


DB = {'key': 'blank', 'name': '처음 연결한 정책표', 'category': '생활지원',
      'data_source_id': '11111111-1111-1111-1111-111111111111'}
URL = 'https://example.invalid/first-policy'
SOURCE = ('처음 등록한 합성 정책\n지원 대상\n유아에게 교육비를 지원합니다.\n'
          '지원 내용\n교육 프로그램 및 교재를 제공합니다.\n'
          '신청 방법\n기관 안내 창구에서 프로그램 신청을 접수합니다.\n')
OPTIONS = {'category': ['생활지원'], 'life_cycle': ['유아'],
           'target_characteristics': ['일반'], 'residence': ['전국']}


def metadata(options):
    properties = {}
    for _key, name, kind in ns.get_policy_props():
        properties[name] = {'type': kind, kind: {'options': [
            {'name': value} for value in options.get(_key, [])]} if kind in
            ('select', 'multi_select') else {}}
    properties.update({name: {'type': 'url', 'url': {}} for name in ns.get_policy_source_props()})
    return {'title': [{'plain_text': DB['name']}], 'properties': properties}


class BlankPolicyTemplateAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='policy-blank-template-')
        database.close_all_connections()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, 'DB_FILE', str(Path(self.temp.name) / 'blank.db')))
        database.init_db()
        ai_verified_input._reset_for_tests()
        self.options = copy.deepcopy(OPTIONS)
        self.pages = []
        self.remote_page = None
        self.stack.enter_context(patch.object(ns, 'get_credentials', return_value=('mock-token', '')))
        self.stack.enter_context(patch.object(ns, 'get_policy_dbs', return_value=[DB]))
        self.stack.enter_context(patch.object(ns, 'resolve_target', new=AsyncMock(return_value={
            'ok': True, 'id': DB['data_source_id'], 'kind': 'data_source',
            'version': ns.DS_VERSION, 'title': DB['name']})))
        self.stack.enter_context(patch.object(ns, '_get', new=AsyncMock(side_effect=self.get_metadata)))
        self.stack.enter_context(patch.object(ns, 'query_policy_source_pages',
            new=AsyncMock(side_effect=lambda *_args: {'ok': True, 'pages': copy.deepcopy(self.pages)})))
        self.create = self.stack.enter_context(patch.object(ns, 'create_policy_page',
            new=AsyncMock(side_effect=self.create_remote)))
        self.stack.enter_context(patch.object(ns, 'get_page',
            new=AsyncMock(side_effect=lambda *_args: copy.deepcopy(self.remote_page))))
        self.write = self.stack.enter_context(patch.object(ns, 'update_page_properties',
            new=AsyncMock(return_value={'ok': True})))
        self.stack.enter_context(patch('aiohttp.ClientSession._request',
            new=AsyncMock(side_effect=AssertionError('Network forbidden in blank-template audit'))))
        self.stack.enter_context(patch.object(ps, '_fetch_source',
            new=AsyncMock(return_value=(SOURCE, SOURCE, False))))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        self.stack.enter_context(patch.object(ai_classifier, 'get_client', return_value=client))
        raw = {'_source_units': {'support_detail': [unit['id'] for unit in source_units(SOURCE)],
               'eligibility_detail': [], 'age_detail': [], 'target_income': []},
               'category': '생활지원', 'life_cycle': ['유아'], 'target_characteristics': [],
               'residence': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}
        def generation(*_args, **kwargs):
            result = copy.deepcopy(raw)
            properties = kwargs['response_json_schema']['properties']
            for key in ('life_cycle', 'target_characteristics', 'residence'):
                allowed = properties[key]['items'].get('enum', [])
                result[key] = [value for value in result[key] if value in allowed]
            if result['category'] not in properties['category'].get('enum', []):
                result['category'] = ''
            return result
        self.generation = self.stack.enter_context(patch.object(ai_classifier, '_call_gemini_json',
            new=AsyncMock(side_effect=generation)))
        self.stack.enter_context(patch.object(ps, '_diff_fields_with_embeddings',
            new=AsyncMock(side_effect=lambda old, new, *_args: ps._diff_fields(old, new, None, {}))))
        real_check = ps.check_policies
        async def no_browser(keys, **kwargs):
            return await real_check(keys, scraper=object(), **kwargs)
        self.stack.enter_context(patch.object(ps, 'check_policies', new=no_browser))
        self.stack.enter_context(patch.object(flow, 'fill_missing_embeddings',
            new=AsyncMock(return_value={'completed': 0, 'failed': 0, 'pause_reason': ''})))

    def tearDown(self):
        ai_verified_input._reset_for_tests()
        database.close_all_connections()
        self.stack.close()
        self.temp.cleanup()

    async def get_metadata(self, *_args):
        return 200, metadata(self.options)

    async def create_remote(self, fields, _source_id):
        self.remote_page = live_page(fields)
        # A Notion GET returns every schema property, including empty values;
        # the create/PATCH builder intentionally omits empty properties.
        for _key, name, kind in ns.get_policy_props():
            self.remote_page['properties'].setdefault(name, {
                'id': 'stable-' + name, 'type': kind,
                kind: [] if kind in ('title', 'rich_text', 'multi_select') else None})
        self.pages = [ns.extract_page_fields(self.remote_page)]
        return {'ok': True, 'page_id': 'audit-page'}

    async def register_first(self, *, support=SOURCE):
        fields = {'title': '처음 등록한 합성 정책', 'category': '생활지원',
                  'life_cycle': ['유아'], 'target_characteristics': [], 'residence': [],
                  'support_detail': support, '_source_urls': [URL]}
        return await api._create_policy_draft(DB, fields)

    def row(self):
        row = database.execute_query('SELECT * FROM notion_policies WHERE page_id=?',
                                     ('audit-page',), fetch_one=True)
        return dict(row) if row else None

    async def test_normal_template_empty_rows_then_first_draft_compares_once(self):
        # A copied template may contain zero policies but keeps its options.
        verified = await ns.verify_policy_databases()
        self.assertTrue(verified['ok'])
        self.assertEqual(verified['items'][0]['option_counts']['life_cycle'], 1)
        synced = await ps.sync_policies(['blank'], defer_embeddings=True)
        self.assertTrue(synced['ok'])
        self.assertEqual(synced['synced'], 0)
        self.assertIsNone(self.row())
        self.assertTrue((await self.register_first())['ok'])
        self.assertIsNone(self.row()['audited_at'])
        first = await ps.run_full_check(['blank'])
        self.assertEqual(first['initial_compared'], 1)
        self.assertEqual(first['changed'], 0)
        self.assertEqual(first['state'], 'completed')
        self.assertIsNotNone(self.row()['audited_at'])
        self.assertTrue(json.loads(self.row()['source_hashes'])[URL])
        self.generation.assert_awaited_once()
        repeat = await ps.run_full_check(['blank'])
        self.assertEqual(repeat['initial_compared'], 0)
        self.generation.assert_awaited_once()
        self.create.assert_awaited_once()
        self.write.assert_not_awaited()

    async def test_first_comparison_proposes_real_difference_without_waiting_for_future_web_change(self):
        await self.register_first(support='이전 교육 프로그램을 제공합니다.')
        result = await ps.run_full_check(['blank'])
        self.assertEqual(result['initial_compared'], 1)
        self.assertEqual(result['changed'], 1)
        self.generation.assert_awaited_once()
        pending = json.loads(self.row()['pending_diff'])
        self.assertTrue(pending['fields'])
        self.assertTrue(all(field['snapshot_current'] for field in pending['fields']))
        self.assertTrue(all(not field.get('applied') for field in pending['fields']))
        self.write.assert_not_awaited()
        selected = await ps.apply_field_to_notion(self.row()['id'], ps._PROP_LABEL['support_detail'])
        self.assertTrue(selected['ok'], selected)
        self.write.assert_awaited_once()

    async def test_explicit_empty_options_never_create_fallback_tag(self):
        # This is NOT the ordinary zero-row template: the user also removed all
        # select/multi-select options. It must not be confused with no schema.
        self.options = {key: [] for key in OPTIONS}
        verified = await ns.verify_policy_databases()
        self.assertTrue(verified['ok'])
        self.assertEqual(verified['items'][0]['option_counts'], {key: 0 for key in OPTIONS})
        await self.register_first()
        self.assertEqual(self.create.await_args.args[0]['life_cycle'], [])
        self.assertEqual(json.loads(self.row()['notion_fields'])['life_cycle'], [])
        first = await ps.run_full_check(['blank'])
        self.assertEqual(first['initial_compared'], 1)
        schema = self.generation.await_args.kwargs['response_json_schema']['properties']
        self.assertNotIn('유아', schema['life_cycle']['items'].get('enum', []))
        self.assertEqual(self.options['life_cycle'], [])
        pending = json.loads(self.row()['pending_diff'] or '{}')
        self.assertFalse(any(field['field'] == ps._PROP_LABEL['life_cycle'] for field in pending.get('fields', [])))
        self.assertEqual(first['changed'], 0)
        self.write.assert_not_awaited()

    async def test_duplicate_same_source_first_registration_sends_once(self):
        first = await self.register_first()
        again = await self.register_first()
        self.assertEqual(first['page_id'], again['page_id'])
        self.assertTrue(again['already_created'])
        self.create.assert_awaited_once()
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS total FROM notion_policies',
                                              fetch_one=True)['total'], 1)

"""Submission safety regressions; synthetic SQLite, no real AI/Notion calls."""
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import database
from services import notion_service as ns, policy_review_store as store, policy_sync as ps
from tests import test_check_policies_baseline_failure as fixtures


class PolicyRecovery(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp
    tearDown = fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown
    _row = fixtures.TestCheckPoliciesBaselineExtractionFailure._row
    _run_check = fixtures.TestCheckPoliciesBaselineExtractionFailure._run_check

    async def test_current_version_unsnapped_field_revalidates_unchanged_source(self):
        source = '지원대상: 등록장애인과 보호자에게 신청 자격'
        url = fixtures._URL
        pending = {'proposal_schema_version': 2, 'intensity': 'high',
                   'extraction_meta': {'prompt_version': ps._EXTRACTION_PROMPT_VERSION},
                   'fields': [{'field': ps._PROP_LABEL['eligibility_detail'],
                               'old': '등록장애인에게 신청 자격', 'new': '등록장애인과 보호자에게 신청 자격',
                               'level': 'high', 'snapshot_current': False}]}
        database.execute_write('UPDATE notion_policies SET notion_fields=?,last_extracted_fields=?,pending_diff=?,change_status=?,source_hashes=?,source_texts=?',
            (json.dumps({'eligibility_detail': '등록장애인에게 신청 자격'}),
             json.dumps({'eligibility_detail': '등록장애인과 보호자에게 신청 자격'}), json.dumps(pending), 'detected',
             json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}), json.dumps({url: source})))
        async def compare(old, new, _client):
            return ps._diff_fields(old, new, None, {})
        with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value={'eligibility_detail': '등록장애인과 보호자에게 신청 자격'})) as ai, \
             patch.object(ps, '_diff_fields_with_embeddings', new=compare):
            await ps.check_policies(['medical'], scraper=object())
        ai.assert_awaited_once()
        self.assertTrue(json.loads(self._row()['pending_diff'])['fields'][0]['snapshot_current'])

    async def test_rolling_ai_baseline_does_not_remove_an_unapplied_previous_field(self):
        old = {'eligibility_detail': '등록장애인에게 신청 자격', 'support_detail': '월 10만원 지급'}
        database.execute_write('UPDATE notion_policies SET notion_fields=?,last_extracted_fields=?',
                               (json.dumps(old), json.dumps(old)))
        first = {'eligibility_detail': '등록장애인과 보호자에게 신청 자격', 'support_detail': '월 10만원 지급'}
        second = dict(first, support_detail='월 20만원 지급')
        async def compare(before, after, _client):
            return ps._diff_fields(before, after, None, {})
        for index, values in enumerate((first, second), 1):
            source = f'지원대상: {values["eligibility_detail"]}\n지원내용: {values["support_detail"]}\n안내 개정 {index}'
            with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
                 patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
                 patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value=values)), \
                 patch.object(ps, '_diff_fields_with_embeddings', new=compare):
                await ps.check_policies(['medical'], scraper=object())
        pending = json.loads(self._row()['pending_diff'])
        by_label = {field['field']: field for field in pending['fields']}
        self.assertIn(ps._PROP_LABEL['eligibility_detail'], by_label)
        self.assertIn(ps._PROP_LABEL['support_detail'], by_label)
        self.assertEqual(by_label[ps._PROP_LABEL['eligibility_detail']]['old'], old['eligibility_detail'])
        self.assertEqual(by_label[ps._PROP_LABEL['eligibility_detail']]['new'], first['eligibility_detail'])

    def test_partly_replaced_source_group_resolves_after_all_current_sources_pass(self):
        policy_id = database.execute_query('SELECT id FROM notion_policies', fetch_one=True)['id']
        old_a, retained_b, new_c = ['https://example.invalid/' + suffix for suffix in ('old-a', 'retained-b', 'new-c')]
        store.record_hold(policy_id, 'Synthetic', [old_a, retained_b], 'ambiguous source group',
                          {'code': 'source_criteria_review_required'})
        # A subset must still not resolve a two-source current group.
        self.assertEqual(store.resolve_after_success(policy_id, [new_c], 'current source', [retained_b, new_c]), 0)
        self.assertEqual(store.resolve_after_success(policy_id, [retained_b, new_c], 'complete current source', [retained_b, new_c]), 1)
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)

    async def test_source_url_replacement_invalidates_even_a_current_version_proposal(self):
        old_url, new_url = fixtures._URL, 'https://replacement.example/policy'
        label = ps._PROP_LABEL['support_detail']
        pending = {'proposal_schema_version': 2, 'intensity': 'high',
                   'extraction_meta': {'prompt_version': ps._EXTRACTION_PROMPT_VERSION},
                   'source_diff': {'url': old_url, 'new': '월 20만원 지급'},
                   'fields': [{'field': label, 'old': '월 10만원 지급', 'new': '월 20만원 지급',
                               'level': 'high', 'snapshot_current': True}]}
        database.execute_write('UPDATE notion_policies SET notion_fields=?,last_extracted_fields=?,pending_diff=?,change_status=?',
            (json.dumps({'support_detail': '월 10만원 지급'}), json.dumps({'support_detail': '월 20만원 지급'}),
             json.dumps(pending), 'detected'))
        ps._upsert_policy('medical', {'page_id': fixtures._PAGE_ID, 'title': '테스트 정책',
                                     'support_detail': '월 10만원 지급', '_source_urls': [new_url]}, None)
        self.assertFalse(ps.get_changes(['medical'])[0]['apply_ready'])
        with patch.object(ps.ai_classifier, 'get_client', return_value=None):
            await ps.recompute_diffs(['medical'])
        self.assertFalse(ps.get_changes(['medical'])[0]['apply_ready'],
                         'Free recomparison cannot approve a replacement source')
        source = '지원내용: 월 30만원 지급'
        async def compare(before, after, _client):
            return ps._diff_fields(before, after, None, {})
        with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value={'support_detail': '월 30만원 지급'})) as ai, \
             patch.object(ps, '_diff_fields_with_embeddings', new=compare):
            await ps.check_policies(['medical'], scraper=object())
        ai.assert_awaited_once()
        new_pending = json.loads(self._row()['pending_diff'])
        self.assertEqual(new_pending['source_diff']['url'], new_url)
        self.assertEqual(new_pending['fields'][0]['new'], '월 30만원 지급')

    async def test_browser_start_failure_releases_child_and_parent_busy(self):
        from scraper_engine import ScraperEngine
        with patch.object(ps, 'sync_policies', new=AsyncMock(return_value={'ok': True})), \
             patch.object(ScraperEngine, 'start', new=AsyncMock(side_effect=OSError('synthetic startup'))), \
             patch.object(ScraperEngine, 'close', new=AsyncMock(side_effect=OSError('synthetic cleanup'))):
            with self.assertRaisesRegex(OSError, 'synthetic startup'):
                await ps.run_full_check(['medical'])
        self.assertFalse(ps.POLICY_STATUS['running'])
        self.assertFalse(ps.ONE_CLICK_STATUS['running'])
        self.assertFalse(ps.SYNC_STATUS['running'])

    async def test_moving_page_preserves_id_history_in_both_database_orders(self):
        for order in (['medical', 'education'], ['education', 'medical']):
            with self.subTest(order=order):
                row = database.execute_query('SELECT id,page_id FROM notion_policies', fetch_one=True)
                pending = {'fields': [{'field': ps._PROP_LABEL['eligibility_detail'], 'old': 'before',
                                      'new': 'after', 'level': 'high', 'applied': True, 'undo_id': 'receipt'}]}
                database.execute_write("UPDATE notion_policies SET db_key='medical',pending_diff=?,change_status='detected'",
                                       (json.dumps(pending),))
                config = [{'key': key, 'name': key, 'data_source_id': key} for key in order]

                async def target(_session, _token, key, *_):
                    return {'ok': True, 'kind': 'data_source', 'id': key}

                async def pages(_session, _token, destination, *_):
                    return {'ok': True, 'pages': [] if destination['id'] == 'medical' else [{
                        'page_id': row['page_id'], 'title': 'Synthetic', 'eligibility_detail': 'after',
                        '_source_urls': ['https://example.invalid/policy']}]}

                async def upsert(key, batch, progress_cb=None):
                    for page in batch:
                        ps._upsert_policy(key, page, None)
                        if progress_cb:
                            progress_cb()

                with patch.object(ns, 'get_credentials', return_value=('synthetic', '')), \
                     patch.object(ns, 'get_policy_dbs', return_value=config), \
                     patch.object(ns, 'resolve_target', new=target), \
                     patch.object(ns, 'get_policy_source_schema', new=AsyncMock(return_value={'ok': True, 'schema': {}})), \
                     patch.object(ns, 'query_policy_source_pages', new=pages), \
                     patch.object(ps, '_embed_and_upsert_batch', new=upsert):
                    result = await ps.sync_policies(order)
                current = database.execute_query('SELECT id,db_key,pending_diff FROM notion_policies', fetch_one=True)
                self.assertTrue(result['ok'])
                self.assertEqual(result['removed'], 0)
                self.assertEqual(current['id'], row['id'])
                self.assertEqual(current['db_key'], 'education')
                self.assertIn('receipt', current['pending_diff'])

    async def test_ai_baseline_is_not_presented_as_notion_snapshot(self):
        actual = '담당자가 작성한 신청 조건'
        old_ai = {'eligibility_detail': '이전 AI 추출 조건'}
        database.execute_write('UPDATE notion_policies SET notion_fields=?,last_extracted_fields=?',
                               (json.dumps({'eligibility_detail': actual}), json.dumps(old_ai)))
        async def extract(*_args, **_kwargs):
            return {'eligibility_detail': '신청 시 주민등록 확인 필요'}
        async def compare(old, new, _client):
            return ps._diff_fields(old, new, None, {})
        with patch.object(ps, '_diff_fields_with_embeddings', new=compare):
            result = await self._run_check(extract)
        self.assertEqual(result['changed'], 1)
        pending = json.loads(self._row()['pending_diff'])
        self.assertEqual(pending['proposal_schema_version'], 2)
        self.assertEqual(pending['fields'][0]['old'], actual)
        self.assertTrue(pending['fields'][0]['snapshot_current'])
        self.assertTrue(ps.get_changes(['medical'])[0]['apply_ready'])

    async def test_replaced_source_success_resolves_old_hold_without_an_old_ai_call(self):
        policy_id = database.execute_query('SELECT id FROM notion_policies', fetch_one=True)['id']
        store.record_hold(policy_id, 'Synthetic', ['https://old.example/policy'], 'ambiguous',
                          {'code': 'source_criteria_review_required'}, 'baseline')
        url = 'https://new.example/policy'
        source = '지원 내용은 매월 교육비 5만원입니다.'
        database.execute_write('UPDATE notion_policies SET source_urls=?, source_hashes=?, source_texts=?, notion_fields=?',
            (json.dumps([url]), json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
             json.dumps({url: source}), json.dumps({'support_detail': source})))
        with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value={'support_detail': source})) as ai, \
             patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])):
            await ps.check_policies(['medical'], scraper=object())
        ai.assert_awaited_once()
        self.assertEqual(store.list_unresolved(['medical'])['total'], 0)
        self.assertEqual(json.loads(self._row()['last_extracted_fields'])['support_detail'], source)

    async def test_notes_are_inline_and_queue_excludes_before_counting(self):
        policy_id = database.execute_query('SELECT id FROM notion_policies', fetch_one=True)['id']
        pending = {'fields': [{'field': ps._PROP_LABEL['eligibility_detail'], 'old': 'old', 'new': 'new', 'level': 'high'}]}
        database.execute_write("UPDATE notion_policies SET pending_diff=?,change_status='detected'", (json.dumps(pending),))
        store.record_hold(policy_id, 'Synthetic', ['https://example.invalid/policy'], 'partial', {'code': 'source_read_incomplete'})
        before = self._row()['pending_diff']
        card = ps.get_changes(['medical'])[0]
        self.assertEqual(card['review_state'], 'source_review')
        self.assertFalse(card['apply_ready'])
        self.assertEqual(len(card['review_notes']), 1)
        self.assertEqual(ps.get_source_reviews(['medical'], 100, 0, [policy_id])['total'], 0)
        self.assertEqual(self._row()['pending_diff'], before)


class MalformedNotionResponse(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_success_is_not_an_empty_database(self):
        class Response:
            status = 200
            def __init__(self, data): self.data = data
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def json(self): return self.data
        class Session:
            def __init__(self, data): self.data = data
            def post(self, *_args, **_kwargs): return Response(self.data)
        for data in ({}, [], {'results': []}, {'results': {}, 'has_more': False},
                     {'results': [{}], 'has_more': False}, {'results': [], 'has_more': 'false'}):
            with self.subTest(data=data):
                result = await ns.query_policy_source_pages(Session(data), 'synthetic', {'kind': 'data_source', 'id': 'fake'})
                self.assertFalse(result['ok'])
                self.assertEqual(result['pages'], [])
        valid = await ns.query_policy_source_pages(Session({'results': [], 'has_more': False}), 'synthetic', {'kind': 'data_source', 'id': 'fake'})
        self.assertTrue(valid['ok'])

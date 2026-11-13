"""Policy connection regressions with disposable DB and fake Notion only."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import database
from services import notion_service as ns, policy_sync as ps, policy_undo as undo
from services.policy_evidence import structured_policy_issues


URL = 'https://example.invalid/first-policy'
SECOND_URL = 'https://example.invalid/eligibility'


def live_page(fields, urls=(URL,)):
    values = {'title': '합성 정책', **fields, '_source_urls': list(urls)}
    properties = ns.build_policy_properties(values)
    for name, prop in properties.items():
        kind = next(iter(prop))
        prop.update(id='stable-' + name, type=kind)
        if kind in ('title', 'rich_text'):
            for item in prop[kind]:
                item['plain_text'] = item['text']['content']
    return {'id': 'audit-page', 'properties': properties}


class PolicyConnectionAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='policy-connection-audit-')
        database.close_all_connections()
        self.db_patch = patch.object(database, 'DB_FILE', str(Path(self.temp.name) / 'audit.db'))
        self.db_patch.start()
        database.init_db()
        self.fields = {'title': '합성 정책', 'support_detail': '교육비를 지원합니다.'}
        self.source = '합성 정책\n지원 내용\n교육비와 교재비를 지원합니다.'
        self.source_texts = {URL: self.source}
        self.label = ps._PROP_LABEL['support_detail']
        self.new = '교육비와 교재비를 지원합니다.'
        self.pd = {'fields': [{'field': self.label, 'old': self.fields['support_detail'],
                   'new': self.new, 'level': 'high', 'snapshot_current': True}],
                   'proposal_schema_version': 2,
                   'extraction_meta': {'prompt_version': ps._EXTRACTION_PROMPT_VERSION},
                   'source_diff': {'url': URL, 'old': '', 'new': self.source}}
        self.insert()

    def tearDown(self):
        database.close_all_connections()
        self.db_patch.stop()
        self.temp.cleanup()

    def insert(self):
        database.execute_write('''INSERT OR REPLACE INTO notion_policies
            (id,db_key,page_id,title,notion_fields,source_urls,source_texts,pending_diff,
             change_status,last_extracted_fields) VALUES (1,'audit','audit-page',?,?,?,?,?,'detected',?)''',
            (self.fields['title'], json.dumps(self.fields), json.dumps(list(self.source_texts)),
             json.dumps(self.source_texts), json.dumps(self.pd),
             json.dumps({'support_detail': self.new})), raise_on_error=True)

    async def test_live_source_or_title_changed_blocks_old_proposal(self):
        # The selected property is unchanged, but the live policy's identity or
        # evidence has been replaced since its saved proposal was generated.
        for changes, urls in (({}, ('https://example.invalid/replacement-policy',)),
                              ({'title': '다른 지원 제도'}, (URL,))):
            with self.subTest(changes=changes, urls=urls):
                self.insert()
                database.execute_write('DELETE FROM policy_apply_receipts') if \
                    database.execute_query("SELECT 1 FROM sqlite_master WHERE name='policy_apply_receipts'", fetch_one=True) else None
                page = live_page({**self.fields, **changes}, urls)
                with patch.object(ns, 'get_page', new=AsyncMock(return_value=page)), \
                     patch.object(ns, 'update_page_properties', new=AsyncMock(return_value={'ok': True})) as write:
                    result = await ps.apply_field_to_notion(1, self.label)
                self.assertFalse(result['ok'], result)
                self.assertEqual(result['error'], 'STALE_SOURCE')
                write.assert_not_awaited()

    async def test_second_configured_source_preserved_at_apply_revalidation(self):
        first = '# 합성 정책\n## 지원 내용\n교육비와 교재비를 지원합니다.'
        second = '# 지원 대상 및 예외\n청소년에게 교육비를 지원합니다.\n중복 수혜자는 제외합니다.'
        # The extraction contract intentionally combines all configured sources.
        combined = first + '\n\n---\n\n' + second
        self.assertNotIn('중복 수혜자는 제외', ps._pick_relevant_snippet(combined, '합성 정책'))
        self.assertEqual(ps._policy_evidence_text(combined, '합성 정책', 2), combined)
        self.assertFalse(structured_policy_issues({'life_cycle': ['청소년']}, combined))
        self.fields['life_cycle'] = []
        self.pd['fields'] = [{'field': ps._PROP_LABEL['life_cycle'], 'old': '', 'new': '청소년',
                              'level': 'high', 'snapshot_current': True}]
        self.source_texts = {URL: first, SECOND_URL: second}
        self.insert()
        with patch.object(ps, '_active_configured_keys', return_value=['audit']), \
             patch.object(ps, '_get_db_schema', return_value={'life_cycle': ['청소년']}), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async',
                          new=AsyncMock(return_value={'life_cycle': ['청소년']})) as extract, \
             patch.object(ps, '_diff_fields_with_embeddings',
                          new=AsyncMock(return_value=copy.deepcopy(self.pd['fields']))), \
             patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
            audit = await ps.run_initial_audit(['audit'])
        self.assertEqual(audit['changed'], 1)
        self.assertEqual(extract.await_args.args[4], combined)
        write.assert_not_awaited()
        stored = database.execute_query('SELECT pending_diff FROM notion_policies WHERE id=1', fetch_one=True)
        self.assertEqual(json.loads(stored['pending_diff'])['source_diff']['url'], URL)
        with patch.object(ps, '_get_db_schema', return_value={'life_cycle': ['청소년']}):
            target, error, _ = ps._apply_field_sync(1, ps._PROP_LABEL['life_cycle'], True, True)
        self.assertIsNone(error)
        self.assertEqual(target['py_value'], ['청소년'])
        context = json.loads(stored['pending_diff'])['source_context']
        self.assertEqual(context['urls'], [URL, SECOND_URL])
        # Missing or changed evidence is held, never silently replaced by a
        # partial source or a concatenated value stored under the first URL.
        database.execute_write('UPDATE notion_policies SET source_texts=? WHERE id=1',
                               (json.dumps({URL: combined}),))
        with patch.object(ps, '_get_db_schema', return_value={'life_cycle': ['청소년']}):
            target, error, _ = ps._apply_field_sync(1, ps._PROP_LABEL['life_cycle'], True, True)
        self.assertIsNone(target)
        self.assertIn('전체 출처 근거', error)

    async def test_changed_source_keeps_unchanged_markdown_companion_in_analysis_and_apply(self):
        old = '# 합성 정책\n## 지원 내용\n교육비를 지원합니다.'
        new = '# 합성 정책\n## 지원 내용\n교육비와 교재비를 지원합니다.'
        companion = '# 지원 대상 및 예외\n청소년에게 교육비를 지원합니다.\n중복 수혜자는 제외합니다.'
        self.fields['life_cycle'] = []
        self.source_texts = {URL: old, SECOND_URL: companion}
        self.pd['source_context'] = ps._proposal_source_context(self.fields['title'], self.source_texts)
        self.pd['fields'] = [{'field': ps._PROP_LABEL['life_cycle'], 'old': '', 'new': '청소년',
                              'level': 'high', 'snapshot_current': True}]
        self.insert()
        database.execute_write('UPDATE notion_policies SET source_hashes=?,audited_at=? WHERE id=1',
            (json.dumps({url: hashlib.sha256(text.encode('utf-8')).hexdigest()
                         for url, text in self.source_texts.items()}), '2026-01-01'))
        async def fetch(_session, _scraper, url, *_args, **_kwargs):
            text = new if url == URL else companion
            return text, text, False
        with patch.object(ps, '_active_configured_keys', return_value=['audit']), \
             patch.object(ps, '_get_db_schema', return_value={'life_cycle': ['청소년']}), \
             patch.object(ps, '_fetch_source', new=AsyncMock(side_effect=fetch)), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async',
                          new=AsyncMock(return_value={'life_cycle': ['청소년']})) as extract, \
             patch.object(ps, '_diff_fields_with_embeddings',
                          new=AsyncMock(return_value=copy.deepcopy(self.pd['fields']))):
            result = await ps.check_policies(['audit'], scraper=object())
            target, error, _ = ps._apply_field_sync(1, ps._PROP_LABEL['life_cycle'], True, True)
        self.assertTrue(result['ok'], result)
        extract.assert_awaited_once()
        self.assertEqual(extract.await_args.args[4], new + '\n\n---\n\n' + companion)
        self.assertIsNone(error)
        self.assertEqual(target['py_value'], ['청소년'])

    async def test_uncertain_receipt_reconciles_after_read_without_replaying(self):
        page = live_page(self.fields)
        prepared, error, _ = ps._apply_field_sync(1, self.label)
        self.assertIsNone(error)
        receipt = undo.prepare(1, self.label, prepared, page, self.fields['support_detail'])
        undo.mark(receipt, 'uncertain')
        # A fresh successful Notion read proves the actual page is still the
        # before value. Simulate the normal sync mirror path with that live data.
        fresh = ns.extract_page_fields(page)
        ps._upsert_policy('audit', fresh, None)
        # Reconciliation is an explicit successful-sync step, not another PATCH.
        database.execute_write('UPDATE notion_policies SET pending_diff=? WHERE id=1',
                               (json.dumps(self.pd),))
        with patch.object(ns, 'get_page', new=AsyncMock(return_value=page)) as read, \
             patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
            result = await undo.reconcile(1)
        self.assertEqual(result, {'resolved': 1, 'pending': 0})
        read.assert_awaited_once()
        write.assert_not_awaited()
        self.assertEqual(undo.applied_records(page_id='audit-page'), [])
        row = database.execute_query('SELECT state FROM policy_apply_receipts WHERE id=?',
                                     (receipt,), fetch_one=True)
        self.assertEqual(row['state'], 'failed')

    async def test_local_recomparison_keeps_exact_source_context(self):
        context = ps._proposal_source_context(self.fields['title'], self.source_texts)
        self.pd['source_context'] = context
        self.insert()
        with patch.object(ps, '_active_configured_keys', return_value=['audit']), \
             patch.object(ps, '_diff_fields', return_value=copy.deepcopy(self.pd['fields'])):
            result = await ps.recompute_diffs(['audit'])
        self.assertEqual(result['updated'], 1)
        stored = database.execute_query('SELECT pending_diff FROM notion_policies WHERE id=1', fetch_one=True)
        self.assertEqual(json.loads(stored['pending_diff'])['source_context'], context)

    async def test_concurrent_same_field_apply_is_serialized_and_sends_once(self):
        page = live_page(self.fields)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def fake_write(*_args):
            entered.set()
            await finish.wait()
            return {'ok': True}
        with patch.object(ns, 'get_page', new=AsyncMock(return_value=page)), \
             patch.object(ns, 'update_page_properties', new=AsyncMock(side_effect=fake_write)) as write:
            first = asyncio.create_task(ps.apply_field_to_notion(1, self.label))
            await asyncio.wait_for(entered.wait(), timeout=3)
            second = asyncio.create_task(ps.apply_field_to_notion(1, self.label))
            await asyncio.sleep(0)
            finish.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
        self.assertTrue(results[0]['ok'])
        self.assertFalse(results[1]['ok'])
        write.assert_awaited_once()

    async def test_source_changed_by_sync_invalidates_old_snapshot_before_write(self):
        # Positive control: the live-source defect above exists specifically
        # before sync. Sync itself correctly invalidates the saved proposal.
        page = live_page(self.fields, ('https://example.invalid/replacement-policy',))
        ps._upsert_policy('audit', ns.extract_page_fields(page), None)
        with patch.object(ns, 'get_page', new=AsyncMock()) as read, \
             patch.object(ns, 'update_page_properties', new=AsyncMock()) as write:
            result = await ps.apply_field_to_notion(1, self.label)
        self.assertFalse(result['ok'])
        self.assertIn('대조하기 전', result['error'])
        read.assert_not_awaited()
        write.assert_not_awaited()

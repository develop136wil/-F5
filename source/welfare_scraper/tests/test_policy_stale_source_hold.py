"""Changed-source failures never authorize an earlier proposal; fake services only."""
import asyncio
from contextlib import ExitStack
import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import database
from services import policy_review_store as reviews, policy_sync as ps
from tests import test_policy_final_connection_audit as fixture


CHANGED = '합성 정책\n지원 내용\n교육비와 교통비를 지원합니다.'
NEW_VALUE = '교육비와 교통비를 지원합니다.'


class ChangedSourceHold(unittest.IsolatedAsyncioTestCase):
    insert = fixture.PolicyConnectionAudit.insert
    tearDown = fixture.PolicyConnectionAudit.tearDown

    def setUp(self):
        fixture.PolicyConnectionAudit.setUp(self)
        self.pd['source_context'] = ps._proposal_source_context(self.fields['title'], self.source_texts)
        self.pd['proposal_schema_version'] = ps._proposal_schema_version()
        self.insert()
        database.execute_write('UPDATE notion_policies SET source_hashes=?,audited_at=? WHERE id=1',
            (json.dumps({fixture.URL: self.digest(self.source)}), '2026-01-01'))

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def row(self):
        return dict(database.execute_query('SELECT * FROM notion_policies WHERE id=1', fetch_one=True))

    def cards(self):
        with patch.object(ps, '_active_configured_keys', return_value=['audit']):
            return ps.get_changes(['audit'])

    async def check(self, response=None, *, source=CHANGED, review=None, error=None,
                    before_extract=None, compare_error=None):
        async def extract(*_args, **kwargs):
            if before_extract:
                before_extract()
            if error:
                raise error
            if review:
                kwargs['review'].update(review)
            return copy.deepcopy(response)
        with ExitStack() as stack:
            stack.enter_context(patch.object(ps, '_active_configured_keys', return_value=['audit']))
            stack.enter_context(patch.object(ps, '_get_db_schema', return_value={}))
            stack.enter_context(patch.object(ps, '_policy_ai_pause_reason', return_value=''))
            stack.enter_context(patch.object(ps.ai_classifier, 'get_client', return_value=object()))
            stack.enter_context(patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))))
            stack.enter_context(patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(side_effect=extract)))
            stack.enter_context(patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(
                side_effect=compare_error or (lambda old, new, *_args: ps._diff_fields(old, new, None, {})))))
            stack.enter_context(patch('aiohttp.ClientSession._request', new=AsyncMock(
                side_effect=AssertionError('Real network forbidden'))))
            stack.enter_context(patch.object(ps.notion_service, 'update_page_properties', new=AsyncMock(
                side_effect=AssertionError('Check must never write Notion'))))
            return await ps.check_policies(['audit'], scraper=object(), include_initial=True, stop_on_quota=True)

    async def assert_blocked(self):
        card = self.cards()[0]
        self.assertFalse(card['apply_ready'])
        self.assertEqual(card['review_state'], 'source_review')
        self.assertIn('source_changed_unverified', {note['code'] for note in card['review_notes']})
        with patch.object(ps.notion_service, 'get_page', new=AsyncMock()) as read, \
             patch.object(ps.notion_service, 'update_page_properties', new=AsyncMock()) as write:
            for force in (False, True):
                result = await ps.apply_field_to_notion(1, self.label, force=force, force_semantic=force)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'NEEDS_SOURCE_REVIEW')
            read.assert_not_awaited()
            write.assert_not_awaited()

    async def test_generic_failure_preserves_retry_baseline_but_blocks_after_restart(self):
        before = self.row()
        result = await self.check()
        self.assertEqual((result['ai_failed'], result['changed']), (1, 0))
        self.assertIn('source_changed_unverified', {note['code'] for note in result['review_notes']})
        after = self.row()
        for key in ('source_hashes', 'source_texts', 'last_extracted_fields', 'pending_diff', 'notion_fields'):
            self.assertEqual(json.loads(before[key]), json.loads(after[key]), key)
        note = reviews.list_unresolved(['audit'])['items'][0]
        self.assertEqual(note['source_hash'], self.digest(CHANGED))
        self.assertNotEqual(note['source_hash'], json.loads(after['source_hashes'])[fixture.URL])
        database.close_all_connections(strict=True)
        await self.assert_blocked()

    async def test_hold_is_durable_before_ai_and_survives_cancellation(self):
        def before_extract():
            database.close_all_connections(strict=True)
            self.assertEqual(reviews.list_unresolved(['audit'])['items'][0]['source_role'], 'freshness')
            self.assertFalse(self.cards()[0]['apply_ready'])
        with self.assertRaises(asyncio.CancelledError):
            await self.check(error=asyncio.CancelledError(), before_extract=before_extract)
        await self.assert_blocked()

    async def test_comparison_exception_keeps_old_proposal_blocked(self):
        before = self.row()
        with self.assertRaisesRegex(RuntimeError, 'synthetic comparison failure'):
            await self.check({'support_detail': NEW_VALUE}, compare_error=RuntimeError('synthetic comparison failure'))
        self.assertEqual(self.row()['pending_diff'], before['pending_diff'])
        await self.assert_blocked()

    async def test_fresh_success_resolves_hold_and_only_new_proposal_can_apply(self):
        await self.check()
        result = await self.check({'support_detail': NEW_VALUE})
        self.assertEqual(result['review_count'], 0)
        self.assertEqual(reviews.list_unresolved(['audit'])['total'], 0)
        self.assertEqual(json.loads(self.row()['source_hashes'])[fixture.URL], self.digest(CHANGED))
        self.assertTrue(self.cards()[0]['apply_ready'])
        self.assertEqual(self.cards()[0]['fields'][0]['new'], NEW_VALUE)
        with patch.object(ps.notion_service, 'get_page', new=AsyncMock(return_value=fixture.live_page(self.fields))), \
             patch.object(ps.notion_service, 'update_page_properties', new=AsyncMock(return_value={'ok': True})) as write:
            applied = await ps.apply_field_to_notion(1, self.label)
        self.assertTrue(applied['ok'], applied)
        write.assert_awaited_once()
        self.assertIn(NEW_VALUE, json.dumps(write.await_args.args[1], ensure_ascii=False))
        self.assertNotIn(self.new, json.dumps(write.await_args.args[1], ensure_ascii=False))

    async def test_fresh_success_matching_notion_retires_old_proposal(self):
        await self.check()
        current = self.fields['support_detail']
        result = await self.check({'support_detail': current}, source='합성 정책\n지원 내용\n' + current)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(reviews.list_unresolved(['audit'])['total'], 0)
        self.assertEqual(self.cards(), [])

    async def test_another_hold_does_not_replace_freshness_or_allow_cached_audit(self):
        await self.check(review={'code': 'source_conditions_incomplete', 'fields': ['support_detail']})
        self.assertEqual({note['source_role'] for note in reviews.list_unresolved(['audit'])['items']},
                         {'current', 'freshness'})
        database.execute_write('UPDATE notion_policies SET audited_at=NULL WHERE id=1')
        before = self.row()
        with patch.object(ps, '_active_configured_keys', return_value=['audit']), \
             patch.object(ps, '_policy_ai_pause_reason', return_value=''), \
             patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
             patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock()) as ai:
            result = await ps.run_initial_audit(['audit'])
        ai.assert_not_awaited()
        self.assertEqual(result['review_count'], 1)
        self.assertIsNone(self.row()['audited_at'])
        self.assertEqual(self.row()['pending_diff'], before['pending_diff'])
        await self.assert_blocked()

    async def test_unchanged_source_temporary_failure_does_not_invalidate_valid_proposal(self):
        # A first-audit marker can be absent on an older, otherwise valid row.
        database.execute_write('UPDATE notion_policies SET audited_at=NULL WHERE id=1')
        before = self.row()
        result = await self.check(source=self.source)
        self.assertEqual(result['ai_failed'], 1)
        self.assertEqual(reviews.list_unresolved(['audit'])['total'], 0)
        self.assertEqual(self.row()['pending_diff'], before['pending_diff'])
        self.assertTrue(self.cards()[0]['apply_ready'])

    async def test_other_policy_is_not_blocked_by_changed_source_failure(self):
        database.execute_write('''INSERT INTO notion_policies
            (id,db_key,page_id,title,notion_fields,source_urls,source_texts,source_hashes,
             pending_diff,change_status,last_extracted_fields,audited_at)
            SELECT 2,'other','other-page',title,notion_fields,source_urls,source_texts,
                   source_hashes,pending_diff,change_status,last_extracted_fields,audited_at
            FROM notion_policies WHERE id=1''')
        await self.check()
        with patch.object(ps, '_active_configured_keys', return_value=['other']):
            other = ps.get_changes(['other'])[0]
        self.assertTrue(other['apply_ready'])
        self.assertEqual(other['review_notes'], [])
        await self.assert_blocked()

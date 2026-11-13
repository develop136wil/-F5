"""Offline create receipts and uncertain-response recovery; no real Notion calls."""
import asyncio
from contextlib import ExitStack, closing
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import api_server as api
from fastapi import HTTPException
from services import policy_draft_state as state, policy_sync as ps, notion_service as ns

URL = 'https://example.invalid/policy'
DB = {'key': 'first', 'data_source_id': 'source-first'}
FIELDS = {'title': '확인한 정책', '_source_urls': [URL]}


class TestDraftState(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')

    def tearDown(self):
        self.conn.close()

    def test_claim_is_global_by_existing_url_normalization_not_selected_database(self):
        first = state.claim(self.conn, URL, DB, FIELDS)
        again = state.claim(self.conn, URL.upper() + '/', {'key': 'other', 'data_source_id': 'other'}, FIELDS)
        self.assertTrue(first['claimed'])
        self.assertFalse(again['claimed'])
        self.assertEqual(again['attempt_id'], first['attempt_id'])
        self.assertEqual(again['source_id'], 'source-first')

    def test_claim_survives_connection_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/receipts.db'
            with closing(sqlite3.connect(path)) as conn:
                first = state.claim(conn, URL, DB, FIELDS)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(state.lookup(conn, URL)['attempt_id'], first['attempt_id'])

    def test_rejection_cannot_release_completed_or_newer_attempt(self):
        first = state.claim(self.conn, URL, DB, FIELDS)
        state.complete(self.conn, URL, first['attempt_id'], 'page-one')
        self.assertFalse(state.release_rejected(self.conn, URL, first['attempt_id']))
        state.allow_retry(self.conn, URL, first['attempt_id'], confirmed=True)
        second = state.claim(self.conn, URL, DB, FIELDS)
        with self.assertRaises(ValueError):
            state.allow_retry(self.conn, URL, first['attempt_id'], confirmed=True)
        with self.assertRaises(ValueError):
            state.complete(self.conn, URL, first['attempt_id'], 'stale-page')
        self.assertEqual(state.lookup(self.conn, URL)['attempt_id'], second['attempt_id'])

    def test_manual_release_requires_exact_true_and_does_not_create(self):
        first = state.claim(self.conn, URL, DB, FIELDS)
        for unconfirmed in (False, 'true', 1, None):
            with self.assertRaises(ValueError):
                state.allow_retry(self.conn, URL, first['attempt_id'], confirmed=unconfirmed)
        self.assertIsNotNone(state.lookup(self.conn, URL))


class TestDraftRecoveryRead(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.attempt = {'source_key': state.source_key(URL), 'source_id': 'original-target', 'page_id': None}
        self.notion = Mock()
        self.notion.resolve_target = AsyncMock(return_value={'ok': True, 'id': 'original-target'})
        self.notion.query_policy_source_pages = AsyncMock(return_value={'ok': True, 'pages': []})
        self.notion.get_page = AsyncMock(return_value=None)

    async def find(self):
        return await state.find_remote(self.attempt, session=object(), token='mock-token', notion=self.notion)

    async def test_recovers_only_complete_read_from_original_target(self):
        self.notion.query_policy_source_pages.return_value = {'ok': True, 'pages': [
            {'page_id': 'created-page', '_source_urls': [URL.upper() + '/']} ]}
        self.assertEqual((await self.find())['page_id'], 'created-page')
        self.assertEqual(self.notion.resolve_target.await_args.args[2], 'original-target')
        self.notion.query_policy_source_pages.return_value['ok'] = False
        self.assertEqual((await self.find())['state'], 'unavailable')

    async def test_completed_page_source_edit_is_not_a_missing_creation(self):
        self.attempt['page_id'] = 'created-page'
        self.notion.query_policy_source_pages.return_value = {'ok': True, 'pages': [
            {'page_id': 'created-page', '_source_urls': ['https://example.invalid/edited']} ]}
        self.assertEqual((await self.find())['state'], 'completed')
        self.notion.get_page.assert_not_awaited()

    async def test_missing_access_to_completed_page_is_not_proof_of_deletion(self):
        self.attempt['page_id'] = 'created-page'
        self.assertEqual((await self.find())['state'], 'unavailable')
        self.notion.get_page.return_value = {'id': 'created-page', 'archived': True}
        self.assertEqual((await self.find())['state'], 'not_found')

    async def test_archived_page_with_new_same_source_returns_existing_page(self):
        self.attempt['page_id'] = 'old-page'
        self.notion.get_page.return_value = {'id': 'old-page', 'archived': True}
        self.notion.query_policy_source_pages.return_value = {'ok': True, 'pages': [
            {'page_id': 'new-page', '_source_urls': [URL]}]}
        self.assertEqual((await self.find())['page_id'], 'new-page')


class TestDraftCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.patches = ExitStack()
        self.mirror = Mock()
        self.create = self.patches.enter_context(patch.object(ns, 'create_policy_page',
            new=AsyncMock(return_value={'ok': True, 'page_id': 'page-one'})))
        self.patches.enter_context(patch.object(ps, 'acquire_policy_mutation_lock', new=AsyncMock(return_value=True)))
        self.patches.enter_context(patch.object(ps, 'release_maintenance_lock'))
        self.patches.enter_context(patch.object(ps, 'normalize_draft_fields'))
        self.schema = self.patches.enter_context(patch.object(api, '_policy_draft_target_schema', new=AsyncMock(return_value={})))
        self.patches.enter_context(patch.object(api, '_policy_draft_state_db', new=self.db))
        self.patches.enter_context(patch.object(api.database, 'run_db', new=self.app_db))

    def tearDown(self):
        self.patches.close()
        self.conn.close()

    async def db(self, function, *args, **kwargs):
        return function(self.conn, *args, **kwargs)

    async def app_db(self, function, *args, **kwargs):
        if function is ps.policy_source_url_exists:
            return False
        if function is ps.record_created_policy:
            return self.mirror(*args, **kwargs)
        raise AssertionError('Unexpected application DB access')

    async def test_success_and_repeated_create_send_once_even_after_database_change(self):
        first = await api._create_policy_draft(DB, FIELDS)
        second = await api._create_policy_draft({'key': 'other', 'data_source_id': 'other'}, FIELDS)
        self.assertEqual(first['page_id'], second['page_id'])
        self.assertTrue(second['already_created'])
        self.create.assert_awaited_once()

    async def test_local_mirror_failure_never_reposts_remote_success(self):
        self.mirror.side_effect = RuntimeError('mock-local-failure')
        first = await api._create_policy_draft(DB, FIELDS)
        self.assertTrue(first['ok'])
        self.assertTrue(first['warning'])
        await api._create_policy_draft(DB, FIELDS)
        self.create.assert_awaited_once()

    async def test_uncertain_response_blocks_automatic_retry(self):
        self.create.return_value = {'ok': False, 'uncertain': True}
        for _ in range(2):
            with self.assertRaises(HTTPException) as error:
                await api._create_policy_draft(DB, FIELDS)
            self.assertEqual(error.exception.status_code, 409)
        self.create.assert_awaited_once()

    async def test_definite_rejection_allows_retry_without_manual_override(self):
        self.create.side_effect = [{'ok': False, 'uncertain': False, 'status': 400},
                                   {'ok': True, 'page_id': 'page-one'}]
        with self.assertRaises(HTTPException):
            await api._create_policy_draft(DB, FIELDS)
        self.assertIsNone(state.lookup(self.conn, URL))
        self.assertTrue((await api._create_policy_draft(DB, FIELDS))['ok'])
        self.assertEqual(self.create.await_count, 2)

    async def test_schema_failure_creates_no_receipt_and_dispatches_nothing(self):
        self.schema.side_effect = HTTPException(409, 'mock-schema-error')
        with self.assertRaises(HTTPException):
            await api._create_policy_draft(DB, FIELDS)
        self.assertIsNone(state.lookup(self.conn, URL))
        self.create.assert_not_awaited()

    async def test_local_payload_rejection_releases_receipt_and_does_not_claim_remote_rejection(self):
        self.create.return_value = dict(ok=False, error='NOTION_PAYLOAD_TOO_LARGE',
            message='원문을 자르지 않았습니다. Notion에는 전송하지 않았습니다.',
            request_sent=False, uncertain=False)
        with self.assertRaises(HTTPException) as raised:
            await api._create_policy_draft(DB, FIELDS)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn('전송하지 않았습니다', raised.exception.detail)
        self.assertIsNone(state.lookup(self.conn, URL))

    async def test_cancellation_during_remote_request_keeps_receipt(self):
        self.create.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await api._create_policy_draft(DB, FIELDS)
        self.assertIsNotNone(state.lookup(self.conn, URL))

    async def test_cancelled_claim_is_drained_and_released_before_any_dispatch(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(function, *args, **kwargs):
            result = await self.db(function, *args, **kwargs)
            if function is state.claim:
                entered.set()
                await finish.wait()
            return result
        with patch.object(api, '_policy_draft_state_db', new=delayed):
            task = asyncio.create_task(api._create_policy_draft(DB, FIELDS))
            await entered.wait()
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(state.lookup(self.conn, URL))
        self.create.assert_not_awaited()

    async def test_receipt_write_failure_keeps_pending_and_can_be_reconciled(self):
        async def fail_complete(function, *args, **kwargs):
            if function is state.complete:
                raise RuntimeError('mock-receipt-write-failure')
            return await self.db(function, *args, **kwargs)
        with patch.object(api, '_policy_draft_state_db', new=fail_complete):
            self.assertTrue((await api._create_policy_draft(DB, FIELDS))['ok'])
        self.assertIsNone(state.lookup(self.conn, URL)['page_id'])
        with self.assertRaises(HTTPException):
            await api._create_policy_draft(DB, FIELDS)
        with patch.object(ns, 'get_credentials', return_value=('mock-token', '')), \
             patch.object(state, 'find_remote', new=AsyncMock(return_value={
                 'state': 'completed', 'page_id': 'page-one', 'fields': FIELDS})):
            result = await api.policy_draft_status_endpoint(api.PolicyDraftStatusReq(url=URL))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(state.lookup(self.conn, URL)['page_id'], 'page-one')
        self.create.assert_awaited_once()

    async def test_explicit_retry_rechecks_and_never_creates_a_page(self):
        first = state.claim(self.conn, URL, DB, FIELDS)
        request = api.PolicyDraftRetryReq(url=URL, attempt_id=first['attempt_id'], confirmed=True)
        with patch.object(api, '_check_policy_draft_receipt', new=AsyncMock(return_value={'state': 'not_found'})):
            result = await api.policy_draft_allow_retry_endpoint(request)
        self.assertTrue(result['ok'])
        self.assertIsNone(state.lookup(self.conn, URL))
        self.create.assert_not_awaited()

    async def test_retry_refuses_created_page_stale_attempt_and_non_boolean_confirmation(self):
        first = state.claim(self.conn, URL, DB, FIELDS)
        for attempt_id, confirmed in ((first['attempt_id'], 'true'), ('stale', True)):
            with self.assertRaises(HTTPException):
                await api.policy_draft_allow_retry_endpoint(api.PolicyDraftRetryReq(
                    url=URL, attempt_id=attempt_id, confirmed=confirmed))
        with patch.object(api, '_check_policy_draft_receipt', new=AsyncMock(return_value={'state': 'completed'})):
            with self.assertRaises(HTTPException):
                await api.policy_draft_allow_retry_endpoint(api.PolicyDraftRetryReq(
                    url=URL, attempt_id=first['attempt_id'], confirmed=True))
        self.assertIsNotNone(state.lookup(self.conn, URL))
        self.create.assert_not_awaited()

    def test_recovery_url_matches_create_without_dns_or_fragment(self):
        self.assertEqual(api._policy_draft_recovery_url(URL + '#section'), URL)

    async def test_invalid_candidate_source_is_rejected_before_schema_or_create(self):
        for url in ('', 'unknown', 'https://user:password@example.invalid/private', 'https://['):
            with self.subTest(url=url), self.assertRaises(HTTPException) as error:
                await api._create_policy_draft(DB, dict(FIELDS, _source_urls=[url]))
            self.assertEqual(error.exception.status_code, 400)
        self.schema.assert_not_awaited()
        self.create.assert_not_awaited()

    async def test_status_has_original_target_name_and_safe_link_after_reconfiguration(self):
        original = {'key': 'first', 'name': '원래 정책표',
                    'data_source_id': 'https://www.notion.so/Test-0123456789abcdef0123456789abcdef?v=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}
        state.claim(self.conn, URL, original, FIELDS)
        with patch.object(api, '_check_policy_draft_receipt', new=AsyncMock(return_value={'state': 'not_found'})):
            result = await api.policy_draft_status_endpoint(api.PolicyDraftStatusReq(url=URL + '#section'))
        self.assertEqual(result['target_name'], '원래 정책표')
        self.assertEqual(result['target_deeplink'], 'https://www.notion.so/0123456789abcdef0123456789abcdef')


class TestNotionCreateResponse(unittest.IsolatedAsyncioTestCase):
    async def test_definite_rejections_and_uncertain_responses_are_distinct(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
        target = {'ok': True, 'kind': 'data_source', 'id': 'target', 'version': 'test'}
        for status, uncertain in ((400, False), (401, False), (403, False), (404, False),
                                  (429, False), (408, True), (500, True), (503, True), (200, True)):
            with self.subTest(status=status), patch.object(ns, 'get_credentials', return_value=('mock-token', '')), \
                 patch.object(ns.aiohttp, 'ClientSession', return_value=Session()), \
                 patch.object(ns, 'resolve_target', new=AsyncMock(return_value=target)), \
                 patch.object(ns, '_post_page', new=AsyncMock(return_value=(status, {}))):
                result = await ns.create_policy_page(FIELDS, 'target')
            self.assertFalse(result['ok'])
            self.assertEqual(result['uncertain'], uncertain)

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import policy_sync as ps
import api_server


class BulkReviewGate(unittest.IsolatedAsyncioTestCase):
    async def test_local_save_failure_stops_and_preserves_error_code(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in (1, 2, 3)])
        responses = [{'ok': True}, {'ok': False, 'error': 'LOCAL_SAVE_FAILED', 'message': '반영은 완료, 기록 확인 필요'}]
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=responses)) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
        self.assertEqual(apply.await_count, 2)
        self.assertEqual(result['failed'][0]['error'], 'LOCAL_SAVE_FAILED')
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['skipped'][0]['reason'], 'NOT_ATTEMPTED')

    async def test_returned_uncertain_update_stops_remaining_items(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in [1, 2, 3]])
        responses = [{'ok': True}, {'ok': False, 'error': 'UPDATE_UNCERTAIN', 'message': '현재 내용을 확인해주세요.'}]
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=responses)) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['failed'][0]['id'], 2)
        self.assertEqual(result['failed'][0]['error'], 'UPDATE_UNCERTAIN')
        self.assertEqual(result['skipped'][0]['id'], 3)
        self.assertEqual(result['skipped'][0]['reason'], 'NOT_ATTEMPTED')
        self.assertEqual(apply.await_count, 2)

    async def test_definitive_rejection_does_not_block_other_selected_fields(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in [1, 2]])
        responses = [{'ok': False, 'error': 'Invalid property', 'http_status': 400}, {'ok': True}]
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=responses)) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(result['failed'][0]['id'], 1)
        self.assertEqual(result['skipped'], [])
        self.assertEqual(apply.await_count, 2)

    async def test_last_item_exception_still_signals_uncertain_to_callers(self):
        label = ps._PROP_LABEL['category']
        for ids in ([1], [1, 2]):
            with self.subTest(ids=ids):
                req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in ids])
                responses = [{'ok': True}] * (len(ids) - 1) + [RuntimeError('private transport details')]
                with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=responses)) as apply:
                    result = await api_server.policy_apply_field_bulk_endpoint(req)
                self.assertEqual(result['applied'], len(ids) - 1)
                self.assertEqual(result['skipped'], [])
                self.assertEqual(result['failed'][0]['id'], ids[-1])
                self.assertEqual(result['failed'][0]['error'], 'UPDATE_UNCERTAIN')
                self.assertNotIn('private', str(result))
                self.assertEqual(apply.await_count, len(ids))

    async def test_busy_reports_every_remaining_item(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in [1, 2, 3]])
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=[{'ok': True}, {'ok': False, 'error': 'MAINTENANCE_BUSY'}])) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
            self.assertEqual(result['applied'], 1)
            self.assertEqual([x['id'] for x in result['skipped']], [2, 3])
            self.assertEqual(apply.await_count, 2)

    async def test_uncertain_error_stops_without_retry_and_keeps_results(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': i, 'field': label} for i in [1, 2, 3]])
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(side_effect=[{'ok': True}, RuntimeError('private transport details')])) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
            self.assertEqual(result['applied'], 1)
            self.assertEqual(result['failed'][0]['id'], 2)
            self.assertEqual(result['skipped'][0]['id'], 3)
            self.assertNotIn('private', str(result))
            self.assertEqual(apply.await_count, 2)

    async def test_duplicate_selection_does_not_write_twice(self):
        label = ps._PROP_LABEL['category']
        req = api_server.PolicyApplyFieldBulkReq(items=[{'id': 1, 'field': label}] * 2)
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(return_value={'ok': True})) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(req)
            self.assertEqual(apply.await_count, 1)
            self.assertEqual(result['skipped'][0]['reason'], 'DUPLICATE')

    async def test_prose_and_unknown_labels_never_reach_notion(self):
        for label in [ps._PROP_LABEL[key] for key in ('support_detail', 'eligibility_detail',
                'residence', 'life_cycle', 'target_characteristics')] + ['알 수 없는 필드']:
            with self.subTest(label=label):
                request = api_server.PolicyApplyFieldBulkReq(items=[{'id': 1, 'field': label}])
                with patch.object(ps, 'apply_field_to_notion', new=AsyncMock()) as apply:
                    result = await api_server.policy_apply_field_bulk_endpoint(request)
                    apply.assert_not_awaited()
                    self.assertEqual(result['applied'], 0)
                    self.assertEqual(result['skipped'][0]['reason'], 'INDIVIDUAL_REVIEW_REQUIRED')

    async def test_tags_still_use_existing_safe_apply_path(self):
        label = ps._PROP_LABEL['category']
        request = api_server.PolicyApplyFieldBulkReq(items=[{'id': 1, 'field': label}])
        with patch.object(ps, 'apply_field_to_notion', new=AsyncMock(return_value={'ok': True})) as apply:
            result = await api_server.policy_apply_field_bulk_endpoint(request)
            apply.assert_awaited_once_with(1, label, force=False)
            self.assertEqual(result['applied'], 1)

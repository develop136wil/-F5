"""Manual first-draft age input probes; synthetic Notion and temporary DB only.

Invalid manually edited ages must be rejected before any Notion write.
"""
import unittest
from unittest.mock import AsyncMock, patch

import api_server as api
from fastapi import HTTPException
from tests import test_policy_blank_template_audit as fixture


class BlankDraftAgeAudit(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.BlankPolicyTemplateAudit.setUp
    tearDown = fixture.BlankPolicyTemplateAudit.tearDown
    get_metadata = fixture.BlankPolicyTemplateAudit.get_metadata
    create_remote = fixture.BlankPolicyTemplateAudit.create_remote

    async def create_age_draft(self, start, end):
        with patch.object(api, '_validate_public_policy_url',
                          new=AsyncMock(return_value=fixture.URL)):
            result = await api.policy_url_draft_create_endpoint(
                api.PolicyUrlDraftCreateReq(url=fixture.URL, db_key=fixture.DB['key'],
                    source_review_confirmed=True,
                    fields={'title': '첫 수동 검토 초안', 'category': '생활지원',
                            'support_detail': '공개 지원 안내',
                            'start_age_months': start, 'end_age_months': end}))
        self.assertTrue(result['ok'], result)
        self.create.assert_awaited_once()
        return self.create.await_args.args[0]

    async def test_reversed_manual_age_range_never_reaches_notion(self):
        with self.assertRaises(HTTPException) as raised:
            await self.create_age_draft(24, 12)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail['error'], 'POLICY_DRAFT_INVALID_AGE_RANGE')
        self.create.assert_not_awaited()

    async def test_malformed_age_never_reaches_notion(self):
        for invalid in ('nan', 'inf', True, '12.5', -1, 1801, []):
            with self.subTest(value=invalid), self.assertRaises(HTTPException) as raised:
                await self.create_age_draft(invalid, None)
            self.assertEqual(raised.exception.status_code, 422)
            self.create.assert_not_awaited()

    async def test_unknown_endpoint_is_allowed(self):
        fields = await self.create_age_draft(12, None)
        self.assertEqual(fields['start_age_months'], 12)
        self.assertIsNone(fields['end_age_months'])

    async def test_valid_manual_age_range_is_preserved(self):
        fields = await self.create_age_draft(12, 24)
        self.assertEqual(fields['start_age_months'], 12)
        self.assertEqual(fields['end_age_months'], 24)

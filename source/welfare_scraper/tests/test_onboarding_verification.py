import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import api_server
from services import notion_service


client = TestClient(api_server.app)


class TestNotionIdNormalization(unittest.TestCase):
    def test_accepts_plain_hyphenated_id(self):
        value = "2738ade5-0210-81f7-94d5-000bdf375781"
        self.assertEqual(notion_service.normalize_notion_id(value), "2738ade5021081f794d5000bdf375781")

    def test_extracts_id_from_full_notion_url(self):
        value = "https://www.notion.so/workspace/Policy-2738ade5021081f794d5000bdf375781?v=abc"
        self.assertEqual(notion_service.normalize_notion_id(value), "2738ade5021081f794d5000bdf375781")

    def test_full_url_ignores_uuid_shaped_view_id(self):
        value = ("https://www.notion.so/workspace/Policy-3cd8ade50210834fafd6877c04a29ef1"
                 "?v=11111111-2222-3333-4444-555555555555")
        self.assertEqual(notion_service.normalize_notion_id(value),
                         "3cd8ade50210834fafd6877c04a29ef1")


class TestPolicyDatabaseVerification(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_returns_actionable_result(self):
        with patch.object(notion_service, "get_credentials", return_value=("", "")):
            result = await notion_service.verify_policy_databases()
        self.assertFalse(result["ok"])
        self.assertIn("토큰", result["error"])

    async def test_missing_database_ids_are_returned_per_database(self):
        with patch.object(notion_service, "get_credentials", return_value=("token", "")), \
             patch.object(notion_service, "get_policy_dbs", return_value=[]):
            result = await notion_service.verify_policy_databases()
        self.assertFalse(result["ok"])
        self.assertEqual(result["items"], [])
        self.assertIn('1개 이상', result['error'])

    async def test_connected_database_reports_detected_custom_option_counts(self):
        class FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False

        properties = {}
        for _fkey, name, ptype in notion_service.get_policy_props():
            definition = {'type': ptype, ptype: {}}
            if ptype in ('select', 'multi_select'):
                definition[ptype]['options'] = [{'name': '선택 1'}, {'name': '선택 2'}]
            properties[name] = definition
        for name in notion_service.get_policy_source_props():
            properties[name] = {'type': 'url', 'url': {}}

        with patch.object(notion_service, 'get_credentials', return_value=('token', '')), \
             patch.object(notion_service, 'get_policy_dbs', return_value=[{
                 'key': 'medical', 'name': '시험', 'data_source_id': 'a' * 32}]), \
             patch.object(notion_service, 'resolve_target', new=AsyncMock(return_value={
                 'ok': True, 'kind': 'data_source', 'id': 'a' * 32,
                 'version': notion_service.DS_VERSION, 'title': '시험'})), \
             patch.object(notion_service, '_get', new=AsyncMock(return_value=(200, {'properties': properties}))), \
             patch('aiohttp.ClientSession', return_value=FakeSession()):
            result = await notion_service.verify_policy_databases()

        self.assertTrue(result['ok'])
        counts = result['items'][0]['option_counts']
        self.assertEqual(counts['category'], 2)
        self.assertEqual(counts['residence'], 2)
        self.assertEqual(counts['target_characteristics'], 2)
        self.assertEqual(counts['life_cycle'], 2)


class TestOnboardingVerificationEndpoints(unittest.TestCase):
    def test_notion_timeout_returns_saved_settings_message(self):
        with patch.object(notion_service, 'verify_connection', new=AsyncMock(side_effect=TimeoutError)):
            response = client.get('/api/notion/verify')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertIn('저장된 설정은 유지', response.json()['error'])

    def test_policy_timeout_does_not_record_partial_failure(self):
        with patch.object(notion_service, 'verify_policy_databases', new=AsyncMock(side_effect=TimeoutError)), \
             patch.object(api_server.database, 'run_db', new=AsyncMock()) as save:
            response = client.get('/api/policy/verify-dbs')
        self.assertFalse(response.json()['ok'])
        self.assertEqual(response.json()['items'], [])
        save.assert_not_called()

    def test_google_verify_without_key(self):
        with patch.object(api_server.utils, "get_env_value", return_value=""):
            response = client.get("/api/config/google/verify")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "missing")

    def test_google_verify_rejects_invalid_model_without_network(self):
        def setting(name):
            return {'GOOGLE_API_KEY': 'secret', 'CLASSIFY_MODEL': '../wrong'}.get(name, '')
        with patch.object(api_server.utils, "get_env_value", side_effect=setting), \
             patch('aiohttp.ClientSession') as session:
            response = client.get("/api/config/google/verify")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "invalid_model")
        session.assert_not_called()

    def test_policy_verify_contract(self):
        expected = {"ok": False, "checked": 5, "items": []}
        with patch.object(notion_service, "verify_policy_databases", new=AsyncMock(return_value=expected)):
            response = client.get("/api/policy/verify-dbs")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["ok"], expected["ok"])
        self.assertEqual(payload["checked"], expected["checked"])
        self.assertEqual(payload["items"], [])
        self.assertTrue(payload["checked_at"])
        self.assertEqual(payload["revision"], api_server.CONFIG_REVISION)


if __name__ == "__main__":
    unittest.main()

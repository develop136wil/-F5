"""동적 Notion 보관함 연결의 안전 경계 회귀 테스트."""
import json
import os
import sys
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import api_server
from services import policy_sync as ps
from services import notion_service as ns


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload


class TestNotionSourceContract(unittest.IsolatedAsyncioTestCase):
    async def test_database_url_resolves_to_new_api_data_source_first(self):
        get = AsyncMock(return_value=(200, {
            "title": [{"plain_text": "정책"}],
            "data_sources": [{"id": "canonical-ds"}],
        }))
        with patch.object(ns, "_get", new=get):
            target = await ns.resolve_target(_FakeSession(), "token", "a" * 32)
        self.assertEqual(target["kind"], "data_source")
        self.assertEqual(target["id"], "canonical-ds")
        self.assertEqual(get.await_count, 1)

    async def test_query_http_failure_is_not_reported_as_empty_success(self):
        session = _FakeSession()
        session.post = lambda *_args, **_kwargs: _FakeResponse(429, {"message": "rate limited"})
        result = await ns.query_policy_source_pages(
            session, "token", {"kind": "data_source", "id": "canonical",
                                "version": ns.DS_VERSION})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["pages"], [])

    async def test_query_429_waits_once_then_recovers(self):
        responses = iter([
            _FakeResponse(429, {"message": "rate limited"}, {"Retry-After": "3"}),
            _FakeResponse(200, {"results": [], "has_more": False}),
        ])
        session = _FakeSession()
        session.post = lambda *_args, **_kwargs: next(responses)
        waits = []
        with patch.object(ns.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await ns.query_policy_source_pages(
                session, "token", {"kind": "data_source", "id": "canonical",
                                    "version": ns.DS_VERSION},
                lambda seconds: waits.append(seconds))
        self.assertTrue(result["ok"])
        self.assertEqual(waits, [3.0])
        sleep.assert_awaited_once_with(3.0)

    async def test_create_page_uses_resolved_canonical_parent(self):
        target = {"ok": True, "kind": "data_source", "id": "canonical-ds",
                  "version": ns.DS_VERSION, "title": "정책"}
        post_page = AsyncMock(return_value=(200, {"id": "page-id"}))
        with patch.object(ns, "get_credentials", return_value=("token", "")), \
             patch.object(ns.aiohttp, "ClientSession", return_value=_FakeSession()), \
             patch.object(ns, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ns, "_post_page", new=post_page):
            result = await ns.create_policy_page({"title": "새 정책"}, "database-url")
        self.assertTrue(result["ok"])
        payload = post_page.await_args.args[2]
        self.assertEqual(payload["parent"], {
            "type": "data_source_id", "data_source_id": "canonical-ds"})


class TestDynamicConnectionSync(unittest.IsolatedAsyncioTestCase):
    async def test_sync_uses_canonical_resolved_data_source(self):
        configured = [{"key": "custom", "name": "사용자 표", "data_source_id": "database-id"}]
        target = {"ok": True, "kind": "data_source", "id": "canonical-ds",
                  "version": "2025-09-03", "title": "사용자 표"}
        run_db = AsyncMock(return_value=[])
        schema = AsyncMock(return_value={"ok": True, "schema": {"category": ["복지"]}})
        pages = AsyncMock(return_value={"ok": True, "pages": []})
        with patch.object(ps.notion_service, "get_credentials", return_value=("token", "")), \
             patch.object(ps.notion_service, "get_policy_dbs", return_value=configured), \
             patch.object(ps.notion_service, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ps.notion_service, "get_policy_source_schema", new=schema), \
             patch.object(ps.notion_service, "query_policy_source_pages", new=pages), \
             patch.object(ps.database, "run_db", new=run_db), \
             patch.object(ps, "_embed_and_upsert_batch", new=AsyncMock()):
            result = await ps.sync_policies(["custom"])

        self.assertTrue(result["ok"])
        self.assertEqual(schema.await_args.args[2]["id"], "canonical-ds")
        self.assertEqual(pages.await_args.args[2]["id"], "canonical-ds")

    async def test_page_fetch_failure_never_deletes_local_mirror(self):
        configured = [{"key": "custom", "name": "사용자 표", "data_source_id": "database-id"}]
        target = {"ok": True, "kind": "data_source", "id": "canonical-ds",
                  "version": "2025-09-03", "title": "사용자 표"}
        run_db = AsyncMock(return_value=[])
        with patch.object(ps.notion_service, "get_credentials", return_value=("token", "")), \
             patch.object(ps.notion_service, "get_policy_dbs", return_value=configured), \
             patch.object(ps.notion_service, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ps.notion_service, "get_policy_source_schema", new=AsyncMock(
                 return_value={"ok": True, "schema": {}})), \
             patch.object(ps.notion_service, "query_policy_source_pages", new=AsyncMock(
                 return_value={"ok": False, "pages": [], "error": "HTTP 429"})), \
             patch.object(ps.database, "run_db", new=run_db), \
             patch.object(ps, "_embed_and_upsert_batch", new=AsyncMock()) as upsert:
            result = await ps.sync_policies(["custom"])

        self.assertFalse(result["ok"])
        self.assertIn("기존 로컬 자료 유지", ps.SYNC_STATUS["message"])
        upsert.assert_not_awaited()
        self.assertFalse(any("DELETE FROM notion_policies" in str(call) for call in run_db.await_args_list))

    async def test_same_canonical_source_is_not_synced_twice(self):
        configured = [
            {"key": "one", "name": "첫 연결", "data_source_id": "database-id"},
            {"key": "two", "name": "둘째 연결", "data_source_id": "data-source-id"},
        ]
        target = {"ok": True, "kind": "data_source", "id": "canonical-ds",
                  "version": "2025-09-03", "title": "정책"}
        pages = AsyncMock(return_value={"ok": True, "pages": []})
        with patch.object(ps.notion_service, "get_credentials", return_value=("token", "")), \
             patch.object(ps.notion_service, "get_policy_dbs", return_value=configured), \
             patch.object(ps.notion_service, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ps.notion_service, "get_policy_source_schema", new=AsyncMock(
                 return_value={"ok": True, "schema": {}})), \
             patch.object(ps.notion_service, "query_policy_source_pages", new=pages), \
             patch.object(ps.database, "run_db", new=AsyncMock(return_value=[])), \
             patch.object(ps, "_embed_and_upsert_batch", new=AsyncMock()):
            result = await ps.sync_policies([])
        self.assertFalse(result["ok"])
        self.assertEqual(pages.await_count, 1)
        self.assertIn("중복", result["failures"][0]["error"])


class TestDynamicConnectionFiltering(unittest.TestCase):
    def test_candidates_only_compare_currently_connected_databases(self):
        calls = []

        def query(sql, params=(), **_kwargs):
            calls.append((sql, params))
            return []

        with patch.object(ps.notion_service, "get_policy_dbs", return_value=[{"key": "active"}]), \
             patch.object(ps.database, "execute_query", side_effect=query):
            self.assertEqual(ps.get_new_candidates(["active"]), [])

        self.assertIn("WHERE db_key IN", calls[0][0])
        self.assertEqual(calls[0][1], ("active",))

    def test_draft_values_not_in_live_schema_are_removed(self):
        schema = {
            "category": ["복지"], "residence": ["도봉구"],
            "target_characteristics": ["일반"], "life_cycle": ["청소년"],
        }
        fields = {
            "category": "없는 분류", "residence": ["없는 지역"],
            "target_characteristics": ["일반", "없는 대상"], "life_cycle": ["노년"],
        }
        with patch.object(ps, "_get_db_schema", return_value=schema), \
             patch.object(ps, "_category_for_db_key", return_value=None):
            ps.normalize_draft_fields(fields, "active", schema)
        self.assertEqual(fields["category"], "")
        self.assertEqual(fields["residence"], [])
        self.assertEqual(fields["target_characteristics"], ["일반"])
        self.assertEqual(fields["life_cycle"], [])

    def test_unfiltered_dashboard_reads_only_current_connection(self):
        calls = []

        def query(sql, params=(), **_kwargs):
            calls.append((sql, params))
            return []

        configured = [{"key": "active", "name": "현재 보관함", "data_source_id": "source"}]
        with patch.object(ps.notion_service, "get_policy_dbs", return_value=configured), \
             patch.object(ps.database, "execute_query", side_effect=query):
            self.assertEqual(ps.get_changes([]), [])
            self.assertEqual(ps.get_health_summary([]), {"dead_links": [], "no_source": []})

        self.assertEqual(len(calls), 2)
        self.assertTrue(all("db_key IN" in sql for sql, _ in calls))
        self.assertTrue(all(params == ("active",) for _, params in calls))

    def test_no_connection_returns_no_policy_candidates_or_dashboard_data(self):
        query = unittest.mock.Mock(side_effect=AssertionError("연결이 없으면 로컬 미러를 조회하면 안 됨"))
        with patch.object(ps.notion_service, "get_policy_dbs", return_value=[]), \
             patch.object(ps.database, "execute_query", query):
            self.assertEqual(ps.get_new_candidates([]), [])
            self.assertEqual(ps.get_changes([]), [])
            self.assertEqual(ps.get_health_summary([]), {"dead_links": [], "no_source": []})
        query.assert_not_called()

    def test_field_apply_uses_single_database_live_options(self):
        label = ps._PROP_LABEL["life_cycle"]
        row = {
            "page_id": "page-1",
            "db_key": "custom",
            "pending_diff": json.dumps({'proposal_schema_version': 2, "extraction_meta": {"prompt_version": ps._EXTRACTION_PROMPT_VERSION}, "fields": [{
                "field": label, "old": "유아", "new": "노년, 존재하지 않는 값", 'snapshot_current': True,
            }]}, ensure_ascii=False),
            "notion_fields": json.dumps({"life_cycle": ["유아"]}, ensure_ascii=False),
            # Live-schema clamping is not itself source evidence. Keep this
            # positive control grounded without weakening its exact options.
            "title": "합성 노인 돌봄 정책",
            "source_texts": json.dumps({"https://example.invalid/synthetic": "노인에게 돌봄 서비스를 지원합니다."}, ensure_ascii=False),
        }
        with patch.object(ps.database, "execute_query", return_value=row), \
             patch.object(ps, "_get_db_schema", return_value={"life_cycle": ["노년"]}):
            target, error, _pending = ps._apply_field_sync(1, label, force=True)
        self.assertIsNone(error)
        self.assertEqual(target["py_value"], ["노년"])
        self.assertEqual(target["properties"]["생애주기"]["multi_select"], [{"name": "노년"}])

    def test_disconnected_preview_never_includes_current_connection(self):
        def query(sql, *_args, **_kwargs):
            if "COUNT(*) AS policy_count" in sql:
                return [{"db_key": "active", "policy_count": 2},
                        {"db_key": "old", "policy_count": 7}]
            if "FROM policy_db_schema" in sql:
                return [{"db_key": "active"}, {"db_key": "old"}]
            if "FROM policy_db_status" in sql:
                return [{"db_key": "active", "title": "현재"},
                        {"db_key": "old", "title": "예전 보관함"}]
            return []

        with patch.object(ps.notion_service, "get_policy_dbs", return_value=[{"key": "active"}]), \
             patch.object(ps.database, "execute_query", side_effect=query):
            result = ps.get_disconnected_policy_data()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["policy_count"], 7)
        self.assertEqual(result["items"][0]["key"], "old")


class TestPolicyConnectionStatusStore(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE policy_db_status (
                db_key TEXT PRIMARY KEY, title TEXT, last_verified_at TIMESTAMP,
                verification_ok INTEGER, last_sync_at TIMESTAMP,
                last_sync_count INTEGER DEFAULT 0, last_error TEXT, updated_at TIMESTAMP
            );
            CREATE TABLE notion_policies (db_key TEXT, page_id TEXT);
            CREATE TABLE policy_db_schema (db_key TEXT PRIMARY KEY, schema_json TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    @contextmanager
    def connection(self):
        yield self.conn

    def query(self, sql, params=(), fetch_one=False):
        rows = self.conn.execute(sql, params).fetchall()
        values = [dict(row) for row in rows]
        return (values[0] if values else None) if fetch_one else values

    def test_verification_and_sync_status_survive_as_one_record(self):
        with patch.object(ps.database, "get_connection", self.connection), \
             patch.object(ps.database, "execute_write", side_effect=lambda sql, params=(), **kwargs: self.conn.execute(sql, params)):
            ps.record_policy_verification([
                {"key": "custom", "name": "임시", "title": "실제 제목", "ok": True}
            ])
            ps.record_policy_sync_result("custom", "실제 제목", True, 12)
        row = dict(self.conn.execute(
            "SELECT * FROM policy_db_status WHERE db_key='custom'").fetchone())
        self.assertEqual(row["title"], "실제 제목")
        self.assertEqual(row["verification_ok"], 1)
        self.assertEqual(row["last_sync_count"], 12)
        self.assertIsNotNone(row["last_verified_at"])
        self.assertIsNotNone(row["last_sync_at"])

    def test_cleanup_deletes_only_disconnected_local_data_after_backup(self):
        self.conn.executemany("INSERT INTO notion_policies VALUES (?,?)", [
            ("active", "a"), ("old", "b"), ("old", "c")])
        self.conn.executemany("INSERT INTO policy_db_schema VALUES (?,?)", [
            ("active", "{}"), ("old", "{}")])
        self.conn.executemany(
            "INSERT INTO policy_db_status (db_key,title) VALUES (?,?)",
            [("active", "현재"), ("old", "예전")])
        self.conn.commit()
        with patch.object(ps.notion_service, "get_policy_dbs", return_value=[{"key": "active"}]), \
             patch.object(ps.database, "get_connection", self.connection), \
             patch.object(ps.database, "execute_query", side_effect=self.query), \
             patch.object(ps.database, "auto_backup_db", return_value=True) as backup:
            result = ps.cleanup_disconnected_policy_data()
        backup.assert_called_once()
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notion_policies WHERE db_key='active'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notion_policies WHERE db_key='old'").fetchone()[0], 0)

    def test_cleanup_stops_without_deleting_when_backup_fails(self):
        self.conn.execute("INSERT INTO notion_policies VALUES ('old','b')")
        self.conn.commit()
        with patch.object(ps.notion_service, "get_policy_dbs", return_value=[]), \
             patch.object(ps.database, "get_connection", self.connection), \
             patch.object(ps.database, "execute_query", side_effect=self.query), \
             patch.object(ps.database, "auto_backup_db", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "백업"):
                ps.cleanup_disconnected_policy_data()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notion_policies").fetchone()[0], 1)


class TestSingleDatabaseCoreFlow(unittest.IsolatedAsyncioTestCase):
    async def test_check_ignores_disconnected_requested_key(self):
        query = unittest.mock.Mock(side_effect=AssertionError("연결 해제된 보관함을 점검하면 안 됨"))
        configured = [{"key": "active", "name": "현재", "data_source_id": "source"}]
        with patch.object(ps.notion_service, "get_policy_dbs", return_value=configured), \
             patch.object(ps.database, "execute_query", query):
            result = await ps.check_policies(["old"], scraper=object())
        self.assertEqual(result, {"ok": True, "checked": 0, "changed": 0})
        query.assert_not_called()

    async def test_single_custom_database_can_create_normalized_draft(self):
        configured = [{
            "key": "custom", "name": "기관 통합 정책", "category": "",
            "data_source_id": "database-url",
        }]
        item = {
            "id": 7,
            "title": "  새 복지 정책  ",
            "link": "https://example.invalid/policy",
            "ai_data": json.dumps({
                "category": "생활지원",
                "residence": ["부산광역시", "없는 지역"],
                "target_characteristics": ["일반", "없는 대상"],
                "life_cycle": ["노년", "없는 생애주기"],
                "start_age_months": 780,
                "end_age_months": None,
                "age_detail": "만 65세 이상",
                "target_income": "소득 기준 없음",
                "eligibility_detail": "부산광역시 거주",
            }, ensure_ascii=False),
            "support_detail": "생활 지원",
            "contact": "대표 문의처",
        }
        schema = {
            "category": ["생활지원"],
            "residence": ["부산광역시"],
            "target_characteristics": ["일반"],
            "life_cycle": ["노년"],
        }
        run_db = AsyncMock(side_effect=[item, False, None, None])
        create_page = AsyncMock(return_value={"ok": True, "page_id": "new-page"})
        target = {"ok": True, "kind": "data_source", "id": "canonical",
                  "version": ns.DS_VERSION, "title": "기관 통합 정책"}

        with patch.object(ns, "get_policy_db", return_value=configured[0]), \
             patch.object(ns, "get_policy_dbs", return_value=configured), \
             patch.object(ns, "get_credentials", return_value=("token", "")), \
             patch.object(ns.aiohttp, "ClientSession", return_value=_FakeSession()), \
             patch.object(ns, "resolve_target", new=AsyncMock(return_value=target)), \
             patch.object(ns, "get_policy_source_schema", new=AsyncMock(
                 return_value={"ok": True, "schema": schema})), \
             patch.object(ns, "create_policy_page", new=create_page), \
             patch.object(ps, "_get_db_schema", return_value=schema), \
             patch.object(api_server, "_policy_draft_state_db", new=AsyncMock(side_effect=[
                 None, {'claimed': True, 'attempt_id': 'attempt-1'}, None])), \
             patch.object(api_server.database, "run_db", new=run_db):
            result = await api_server.policy_draft_endpoint(
                api_server.PolicyDraftReq(item_id=7, db_key="custom"))

        self.assertEqual(result["page_id"], "new-page")
        fields, destination = create_page.await_args.args
        self.assertEqual(destination, "database-url")
        self.assertEqual(fields["title"], "새 복지 정책")
        self.assertEqual(fields["category"], "생활지원")
        self.assertEqual(fields["residence"], ["부산광역시"])
        self.assertEqual(fields["target_characteristics"], ["일반"])
        self.assertEqual(fields["life_cycle"], ["노년"])
        self.assertEqual(fields["_source_urls"], ["https://example.invalid/policy"])


class TestDuplicateConnectionValidation(unittest.IsolatedAsyncioTestCase):
    async def test_same_notion_database_with_different_keys_is_rejected(self):
        first = "https://www.notion.so/Policy-3cd8ade50210834fafd6877c04a29ef1"
        second = "3cd8ade5-0210-834f-afd6-877c04a29ef1"
        value = (f'['
                 f'{{"key":"one","data_source_id":"{first}"}},'
                 f'{{"key":"two","data_source_id":"{second}"}}]')
        with patch.object(api_server.utils, "set_env_values") as save:
            with self.assertRaises(HTTPException) as error:
                await api_server.save_config(api_server.ConfigRequest(
                    items={"NOTION_POLICY_DBS": value}))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("두 번", error.exception.detail)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()

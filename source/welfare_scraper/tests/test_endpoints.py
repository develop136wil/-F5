"""엔드포인트 통합 테스트 (FastAPI TestClient).

신규 백엔드 엔드포인트(의미검색·임베딩 백필·스케줄·OS작업)의 계약/검증을 확인한다.
임베딩 API는 모킹하여 네트워크/키 없이 실행. TestClient를 컨텍스트 매니저로 쓰지 않아
lifespan(스케줄러/백업)은 기동하지 않는다(엔드포인트 단위 검증에 집중).
"""
import os
import sqlite3
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import api_server
import history_manager
from services import embedding_service, ai_coordinator

client = TestClient(api_server.app)


def setUpModule():
    # Status now refreshes the shared ledger. Even a single selected endpoint
    # test must never read or create a quota/job file in the user's profile.
    temporary = tempfile.TemporaryDirectory(prefix='endpoint-ai-state-')
    unittest.addModuleCleanup(temporary.cleanup)
    for attribute, filename in (('_state_path', 'quota.json'), ('_job_lock_path', 'ai_job.lock')):
        isolated = patch.object(ai_coordinator, attribute, return_value=os.path.join(temporary.name, filename))
        isolated.start()
        unittest.addModuleCleanup(isolated.stop)
    ai_coordinator._reset_for_tests()
    unittest.addModuleCleanup(ai_coordinator._reset_for_tests)


class TestScheduleEndpoints(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_path_patch = patch(
            'utils.get_config_path',
            return_value=os.path.join(self.config_dir.name, 'config.json'))
        self.config_path_patch.start()

    def test_get_schedule(self):
        r = client.get("/api/schedule")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("enabled", body)
        self.assertIn("mode", body)

    def test_set_schedule_valid_daily(self):
        r = client.post("/api/schedule", json={"enabled": True, "mode": "daily", "time": "08:30", "interval_hours": 6})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "daily")

    def test_set_schedule_invalid_mode(self):
        r = client.post("/api/schedule", json={"enabled": True, "mode": "weekly", "time": "08:30"})
        self.assertEqual(r.status_code, 400)

    def test_set_schedule_invalid_time(self):
        r = client.post("/api/schedule", json={"enabled": True, "mode": "daily", "time": "99:99"})
        self.assertEqual(r.status_code, 400)

    def test_set_schedule_invalid_interval(self):
        r = client.post("/api/schedule", json={"enabled": True, "mode": "interval", "interval_hours": 999})
        self.assertEqual(r.status_code, 400)

    def tearDown(self):
        # 테스트가 남긴 예약을 비활성화로 복구
        client.post("/api/schedule", json={"enabled": False, "mode": "daily", "time": "09:00"})
        self.config_path_patch.stop()
        self.config_dir.cleanup()


class TestDeferredScheduleCollision(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_retry_does_not_release_the_active_ai_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            ai_coordinator, "_job_lock_path", return_value=os.path.join(tmp, "ai_job.lock")
        ):
            ai_coordinator._reset_for_tests()
            self.assertTrue(await ai_coordinator.try_start_job("policy_check", "정책 점검"))
            try:
                started = await api_server._scheduled_scrape_job(allow_defer=False)
                self.assertFalse(started)
                self.assertEqual(ai_coordinator.status()["job_kind"], "policy_check")
                self.assertFalse(api_server.SCRAPER_STATUS["is_running"])
            finally:
                ai_coordinator.finish_job("policy_check")
                ai_coordinator._reset_for_tests()


class TestOsTaskEndpoint(unittest.TestCase):
    def test_get_os_task_shape(self):
        r = client.get("/api/schedule/os-task")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("supported", body)
        self.assertIn("registered", body)


class TestEmbeddingEndpoints(unittest.TestCase):
    def setUp(self):
        self.ai_tmp = tempfile.TemporaryDirectory()
        self.ai_lock_patch = patch.object(
            ai_coordinator, "_job_lock_path",
            return_value=os.path.join(self.ai_tmp.name, "ai_job.lock"),
        )
        self.ai_lock_patch.start()
        ai_coordinator._reset_for_tests()

    def tearDown(self):
        ai_coordinator._reset_for_tests()
        self.ai_lock_patch.stop()
        self.ai_tmp.cleanup()

    def test_embeddings_status_shape(self):
        r = client.get("/api/embeddings/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for k in ("total", "embedded", "missing"):
            self.assertIn(k, body)

    def test_semantic_search_without_key_keeps_keyword_fallback(self):
        # API 키가 없어도 전체 기능이 멈추지 않고 정확 단어 검색으로 저하 운전한다.
        with patch.object(embedding_service, "_get_client", return_value=None), \
             patch.object(history_manager, "hybrid_search", return_value={"items": [], "total": 0}):
            r = client.get("/api/search/semantic", params={"q": "영유아 보육"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["semantic_unavailable_reason"], "missing_api_key")

    def test_semantic_search_with_mocked_embedding(self):
        async def fake_embed(text, task_type='RETRIEVAL_QUERY', client=None):
            return [0.1, 0.2, 0.3]

        fake_result = {"items": [{"id": 1, "title": "보육료 지원", "score": 0.9}], "total": 1}
        # (2026-08-05) _get_client도 함께 모킹 — 환경에 실제 GOOGLE_API_KEY가 없으면
        # get_unavailable_reason()이 'missing_api_key'로 판정해 embed_text_async(모킹된
        # 성공 경로)에 도달하기도 전에 503이 나 이 테스트가 환경에 암묵적으로 의존하게 된다.
        # (2026-08-07, GPT Codex 리뷰로 발견) 같은 이유로 _daily_quota_exhausted_today도
        # 함께 고정해야 한다 — 실제 개발 환경에 그날 일일 임베딩 한도 소진 상태가
        # 영속화(quota_state.json)돼 있으면 이 성공 경로 테스트도 503을 받아버린다.
        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=False), \
             patch.object(embedding_service, "embed_text_async", side_effect=fake_embed), \
             patch.object(history_manager, "hybrid_search", return_value=fake_result):
            r = client.get("/api/search/semantic", params={"q": "영아 양육 도움", "limit": 5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["title"], "보육료 지원")

    def test_semantic_search_daily_quota_exhausted_has_distinct_message(self):
        """(2026-08-05, GPT Codex 3차 리뷰) 일일 한도 소진과 API 키 없음이 같은 메시지를
        쓰면 사용자가 키를 재발급하는 등 잘못된 조치를 할 수 있다 — 메시지가 달라야 한다."""
        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=True), \
             patch.object(history_manager, "hybrid_search", return_value={"items": [], "total": 0}):
            r = client.get("/api/search/semantic", params={"q": "영유아 보육"})
        self.assertEqual(r.status_code, 200)
        detail = r.json()["notice"]
        self.assertIn("일일 한도", detail)
        self.assertNotIn("API 키", detail)

    def test_semantic_search_missing_key_message_differs_from_quota(self):
        with patch.object(embedding_service, "_get_client", return_value=None), \
             patch.object(history_manager, "hybrid_search", return_value={"items": [], "total": 0}):
            r = client.get("/api/search/semantic", params={"q": "영유아 보육"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("API 키", r.json()["notice"])

    def test_backfill_without_key_returns_503(self):
        with patch.object(embedding_service, "_get_client", return_value=None):
            r = client.post("/api/embeddings/backfill", params={"batch": 10})
        self.assertEqual(r.status_code, 503)

    def test_backfill_daily_quota_exhausted_returns_503_before_any_api_call(self):
        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=True), \
             patch.object(embedding_service, "embed_texts_batch_async") as mock_batch:
            r = client.post("/api/embeddings/backfill", params={"batch": 10})
        self.assertEqual(r.status_code, 503)
        self.assertIn("일일 한도", r.json()["detail"])
        mock_batch.assert_not_called()

    def test_backfill_chunks_large_batch_and_reports_failed_count(self):
        """25건 요청 시 청크 20개 단위로 나뉘어 2번 호출되고, 실패 청크는 failed로 집계."""
        fake_items = [{"id": i + 1, "link": f"https://x.example/{i}", "title": f"항목{i}",
                       "support_detail": ""} for i in range(25)]
        call_sizes = []

        async def fake_batch(texts, *a, **k):
            call_sizes.append(len(texts))
            if len(call_sizes) == 1:
                return [None] * len(texts)  # 첫 청크(20건) 실패
            return [[0.1, 0.2]] * len(texts)  # 둘째 청크(5건) 성공

        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=False), \
             patch.object(history_manager, "get_items_missing_embedding", return_value=fake_items), \
             patch.object(embedding_service, "embed_texts_batch_async", side_effect=fake_batch), \
             patch.object(history_manager, "save_embedding", return_value=True):
            r = client.post("/api/embeddings/backfill", params={"batch": 25})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(call_sizes, [20, 5])
        body = r.json()
        self.assertEqual(body["embedded"], 5)
        self.assertEqual(body["failed"], 20)

    def test_backfill_counts_database_save_failure(self):
        """벡터 생성 성공과 DB 저장 성공을 구분해 실제 저장 건수만 보고한다."""
        fake_items = [{"id": 1, "link": "https://x.example/save-fail", "title": "항목",
                       "support_detail": ""}]

        async def fake_batch(texts, *a, **k):
            return [[0.1, 0.2]]

        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=False), \
             patch.object(history_manager, "get_items_missing_embedding", return_value=fake_items), \
             patch.object(embedding_service, "embed_texts_batch_async", side_effect=fake_batch), \
             patch.object(history_manager, "save_embedding",
                          side_effect=sqlite3.OperationalError("disk full")), \
             patch.object(history_manager, "count_embeddings",
                          return_value={"total": 1, "embedded": 0, "missing": 1}):
            r = client.post("/api/embeddings/backfill", params={"batch": 1})

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["embedded"], 0)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["remaining"], 1)

    def test_backfill_reports_unavailable_reason_when_quota_exhausted_mid_run(self):
        """(2026-08-05, GPT Codex 4차 리뷰) 백필 도중(첫 청크에서) 일일 한도를 새로
        소진한 경우, 응답에 unavailable_reason을 실어 프론트가 "그냥 실패"와 "한도 소진"을
        구분할 수 있게 해야 한다."""
        fake_items = [{"id": i + 1, "link": f"https://x.example/{i}", "title": f"항목{i}",
                       "support_detail": ""} for i in range(5)]
        quota_state = {"exhausted": False}

        async def fake_batch(texts, *a, **k):
            quota_state["exhausted"] = True  # 이 청크 처리 중 한도 소진됐다고 가정
            return [None] * len(texts)

        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today",
                          side_effect=lambda: quota_state["exhausted"]), \
             patch.object(history_manager, "get_items_missing_embedding", return_value=fake_items), \
             patch.object(embedding_service, "embed_texts_batch_async", side_effect=fake_batch):
            r = client.post("/api/embeddings/backfill", params={"batch": 5})

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["failed"], 5)
        self.assertEqual(body["unavailable_reason"], "daily_quota_exhausted")

    def test_backfill_no_reason_when_failure_is_not_quota_related(self):
        fake_items = [{"id": 1, "link": "https://x.example/1", "title": "항목1", "support_detail": ""}]

        async def failing_batch(texts, *a, **k):
            raise RuntimeError("일시적 네트워크 오류")

        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today", return_value=False), \
             patch.object(history_manager, "get_items_missing_embedding", return_value=fake_items), \
             patch.object(embedding_service, "embed_texts_batch_async", side_effect=failing_batch):
            r = client.post("/api/embeddings/backfill", params={"batch": 1})

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["failed"], 1)
        self.assertIsNone(body["unavailable_reason"])

    def test_semantic_search_detects_quota_exhausted_on_first_failing_request(self):
        """(2026-08-05, GPT Codex 4차 리뷰) 사전 판정 통과 후, 실제 embed 호출 안에서
        '처음' 일일 한도에 도달한 요청도(내부적으로 그 호출 중 소진 플래그가 세워짐)
        첫 요청부터 전용 문구를 받아야 한다 — "일시적 오류"로 뭉뚱그리면 안 됨."""
        quota_state = {"exhausted": False}

        async def fake_embed(text, task_type='RETRIEVAL_QUERY', client=None):
            quota_state["exhausted"] = True  # 이 호출 도중 한도를 소진했다고 가정
            return None

        with patch.object(embedding_service, "_get_client", return_value=object()), \
             patch.object(embedding_service, "_daily_quota_exhausted_today",
                          side_effect=lambda: quota_state["exhausted"]), \
             patch.object(embedding_service, "embed_text_async", side_effect=fake_embed), \
             patch.object(history_manager, "hybrid_search", return_value={"items": [], "total": 0}):
            r = client.get("/api/search/semantic", params={"q": "영유아 보육"})

        self.assertEqual(r.status_code, 200)
        detail = r.json()["notice"]
        self.assertIn("일일 한도", detail)
        self.assertNotIn("일시적 오류", detail)


class TestAiBacklogEndpoint(unittest.TestCase):
    def test_ai_backlog_status_shape(self):
        r = client.get("/api/ai-backlog/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("backlog_total", body)
        self.assertIn("permanently_excluded", body)


class TestPolicyWatchEndpoints(unittest.TestCase):
    def test_list_shape(self):
        r = client.get("/api/policy-watch")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_add_invalid_url(self):
        r = client.post("/api/policy-watch", json={"name": "x", "url": "not-a-url", "scope": ""})
        self.assertEqual(r.status_code, 400)

    def test_check_mocked(self):
        from services import policy_watch as pw
        async def fake_check(scraper=None):
            return {"checked": 2, "changed": 1, "items": []}
        with patch.object(pw, "run_watch_check", side_effect=fake_check):
            r = client.post("/api/policy-watch/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["changed"], 1)


class TestStatusEndpoint(unittest.TestCase):
    def test_status_has_ai_savings_field(self):
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ai_savings", r.json())
        self.assertIn("save_result", r.json())

    def test_global_ai_status_shape(self):
        r = client.get("/api/ai/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("busy", data)
        self.assertIn("job_label", data)
        self.assertIn("models", data)


if __name__ == "__main__":
    unittest.main()

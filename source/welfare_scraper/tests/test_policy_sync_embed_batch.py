"""policy_sync._embed_and_upsert_batch 전용 테스트 (실제 임시 DB 사용).

(2026-08-05, GPT Codex 리뷰로 발견) 기존엔 정책 제목 임베딩을 페이지마다 개별 API
호출로 보내 분당 한도(RPM) 위험이 있었다 — 청크 배치 호출로 바꾼 뒤 그 동작을 검증한다.
test_policy_sync.py는 "DB/네트워크 비의존 순수 함수만" 검증한다고 명시돼 있어, DB에
실제로 쓰는 이 로직은 별도 파일로 분리한다(test_e2e.py와 같은 임시 DB 격리 패턴 사용).
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services import policy_sync as ps


class TestEmbedAndUpsertBatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_db = 'test_policy_embed_batch.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def _row(self, page_id):
        return database.execute_query(
            "SELECT title, title_embedding FROM notion_policies WHERE page_id=?",
            (page_id,), fetch_one=True)

    async def test_batches_titles_in_chunks_of_configured_size(self):
        """25건이면 청크 크기 20 기준으로 배치 API가 정확히 2번(20+5) 호출돼야 한다."""
        pages = [{"page_id": f"p{i}", "title": f"정책 {i}"} for i in range(25)]
        call_sizes = []

        async def fake_batch(texts, *a, **k):
            call_sizes.append(len(texts))
            return [[0.1, 0.2]] * len(texts)

        with patch('services.embedding_service.embed_texts_batch_async', side_effect=fake_batch):
            await ps._embed_and_upsert_batch("medical", pages)

        self.assertEqual(call_sizes, [20, 5])
        for i in range(25):
            row = self._row(f"p{i}")
            self.assertIsNotNone(row)
            self.assertIsNotNone(row["title_embedding"])

    async def test_unchanged_titles_are_reused_without_api_call(self):
        pages = [{"page_id": "p1", "title": "동일 제목 정책"}]
        calls = []

        async def fake_batch(texts, *a, **k):
            calls.append(texts)
            return [[0.1, 0.2, 0.3]] * len(texts)

        with patch('services.embedding_service.embed_texts_batch_async', side_effect=fake_batch):
            await ps._embed_and_upsert_batch("medical", pages)
        self.assertEqual(len(calls), 1)  # 최초 1회는 임베딩 필요

        with patch('services.embedding_service.embed_texts_batch_async', side_effect=fake_batch):
            await ps._embed_and_upsert_batch("medical", pages)
        self.assertEqual(len(calls), 1, "제목이 안 바뀌었으면 재임베딩 API를 호출하면 안 된다")

    async def test_one_failed_chunk_does_not_affect_other_chunk_or_reuse(self):
        """청크 하나가 통째로 실패해도(예외) 다른 청크·재사용 항목은 정상 처리돼야 한다."""
        # 먼저 p_reuse를 정상 임베딩해둔다.
        async def ok_batch(texts, *a, **k):
            return [[0.1]] * len(texts)
        with patch('services.embedding_service.embed_texts_batch_async', side_effect=ok_batch):
            await ps._embed_and_upsert_batch("medical", [{"page_id": "p_reuse", "title": "재사용 정책"}])

        # 25건: 앞 20개는 실패, 뒤 5개(다른 청크)는 성공 + 재사용 1건 섞어서 재실행
        new_pages = [{"page_id": f"new{i}", "title": f"신규 {i}"} for i in range(25)]
        pages = [{"page_id": "p_reuse", "title": "재사용 정책"}] + new_pages

        call_n = [0]

        async def flaky_batch(texts, *a, **k):
            call_n[0] += 1
            if call_n[0] == 1:
                raise RuntimeError("일시적 네트워크 오류")
            return [[0.9]] * len(texts)

        with patch('services.embedding_service.embed_texts_batch_async', side_effect=flaky_batch):
            await ps._embed_and_upsert_batch("medical", pages)

        # 재사용 항목은 여전히 임베딩 보존
        self.assertIsNotNone(self._row("p_reuse")["title_embedding"])
        # 실패한 첫 청크(new0~new19)는 임베딩 없이 저장(레코드는 존재)
        self.assertIsNone(self._row("new0")["title_embedding"])
        self.assertIsNotNone(self._row("new0"))
        # 성공한 두번째 청크(new20~new24)는 임베딩 정상 저장
        self.assertIsNotNone(self._row("new24")["title_embedding"])

    async def test_progress_callback_called_once_per_page(self):
        pages = [{"page_id": f"p{i}", "title": f"정책 {i}"} for i in range(7)]
        count = [0]

        async def fake_batch(texts, *a, **k):
            return [[0.1]] * len(texts)

        with patch('services.embedding_service.embed_texts_batch_async', side_effect=fake_batch):
            await ps._embed_and_upsert_batch("medical", pages, progress_cb=lambda: count.__setitem__(0, count[0] + 1))
        self.assertEqual(count[0], 7)

    async def test_title_changed_and_embed_failed_clears_stale_embedding(self):
        """(2026-08-05, GPT Codex 3차 리뷰) 제목이 실제로 바뀌었는데 새 임베딩 생성이
        실패하면, 옛 제목의 벡터를 그대로 남기지 말고 비워야 한다(새 제목과 안 맞는
        벡터가 유사도 판정에 잘못 쓰이는 것을 막기 위함)."""
        async def ok_batch(texts, *a, **k):
            return [[0.1, 0.2]] * len(texts)
        with patch('services.embedding_service.embed_texts_batch_async', side_effect=ok_batch):
            await ps._embed_and_upsert_batch(
                "medical", [{"page_id": "p_rename", "title": "옛날 제목"}])
        self.assertIsNotNone(self._row("p_rename")["title_embedding"])

        async def failing_batch(texts, *a, **k):
            return [None] * len(texts)  # 임베딩 생성 실패(429 등)
        with patch('services.embedding_service.embed_texts_batch_async', side_effect=failing_batch):
            await ps._embed_and_upsert_batch(
                "medical", [{"page_id": "p_rename", "title": "새로 바뀐 제목"}])

        row = self._row("p_rename")
        self.assertEqual(row["title"], "새로 바뀐 제목")
        self.assertIsNone(row["title_embedding"],
                          "제목이 바뀌고 재임베딩도 실패했으면 옛 벡터를 비워야 한다")

    async def test_title_unchanged_and_embed_failed_preserves_embedding(self):
        """비교 대조군: 제목이 안 바뀐 상태에서의 실패는 기존 벡터를 보존해야 한다
        (재사용 로직이 애초에 이 경우 API를 다시 안 부르지만, 방어 코드 자체를 검증)."""
        async def ok_batch(texts, *a, **k):
            return [[0.1, 0.2]] * len(texts)
        with patch('services.embedding_service.embed_texts_batch_async', side_effect=ok_batch):
            await ps._embed_and_upsert_batch(
                "medical", [{"page_id": "p_same", "title": "안 바뀐 제목"}])
        original_blob = self._row("p_same")["title_embedding"]
        self.assertIsNotNone(original_blob)

        # _upsert_policy를 직접 호출해 "제목 동일 + blob=None" 경로를 강제 재현
        await database.run_db(ps._upsert_policy, "medical",
                              {"page_id": "p_same", "title": "안 바뀐 제목"}, None)
        row = self._row("p_same")
        self.assertEqual(row["title_embedding"], original_blob,
                         "제목이 안 바뀌었으면 실패해도 기존 벡터를 보존해야 한다")

    async def test_field_comparison_uses_one_batch_embedding_request(self):
        old = {
            "eligibility_detail": "서울 거주 가구",
            "target_income": "중위소득 80% 이하",
            "support_detail": "월 10만원 지원",
        }
        new = {
            "eligibility_detail": "서울시에 사는 가구",
            "target_income": "기준중위소득 80퍼센트 이하",
            "support_detail": "매월 10만원을 지원합니다",
        }
        batch = AsyncMock(side_effect=lambda texts, **_kwargs: [[1.0, float(i)] for i, _ in enumerate(texts)])
        with patch('services.embedding_service.embed_texts_batch_async', batch), \
             patch('services.embedding_service.embed_text', side_effect=AssertionError("개별 호출 금지")):
            diffs = await ps._diff_fields_with_embeddings(old, new, object())
        batch.assert_awaited_once()
        requested = batch.await_args.args[0]
        self.assertEqual(len(requested), len(set(requested)))
        self.assertGreaterEqual(len(diffs), 3)


if __name__ == '__main__':
    unittest.main()

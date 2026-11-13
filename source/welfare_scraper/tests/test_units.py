"""순수 함수 단위 테스트.

DB/네트워크/브라우저에 의존하지 않는 로직(검증, 날짜 파싱, 병합, Rate Limiter)을
빠르게 검증한다. `python -m unittest tests.test_units` 로 실행.
"""
import unittest
from unittest.mock import patch
import time
import os
import sys
import datetime

# 상위 디렉터리를 sys.path에 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import utils
import database
import history_manager
from scraper_engine import ScraperEngine
from services.scraper_service import GeminiRateLimiter, _extract_page_modified_date


class TestInitDbFreshDatabase(unittest.TestCase):
    """(2026-07-20) idx_content_hash 생성이 content_hash 컬럼 추가보다 먼저 실행되던
    버그의 회귀 테스트 — 완전 신규(빈) DB에서 init_db()가 예외 없이 인덱스까지
    전부 만들어내는지 확인한다."""

    def setUp(self):
        self.test_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fresh_init_test.db')
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        database.close_all_connections()
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        database.close_all_connections()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_fresh_db_creates_content_hash_index(self):
        database.init_db()
        with database.get_connection() as conn:
            cols = {row['name'] for row in conn.execute("PRAGMA table_info(welfare_items)")}
            self.assertIn('content_hash', cols)
            indexes = {row['name'] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='welfare_items'")}
            self.assertIn('idx_content_hash', indexes)


class TestEmbeddingBlobNotLeakedToApi(unittest.TestCase):
    """(버그 2026-07-21) welfare_items.embedding은 float32 BLOB(bytes)인데, 조회 함수들이
    `SELECT *` 결과를 그대로 반환해 API가 그대로 JSON 응답하면 FastAPI 인코더가 bytes를
    직렬화하지 못해 500이 났다 — 실측: 임베딩이 채워진 항목이 있으면 /api/data,
    /api/data/{id}, /api/search가 전부 이 경로로 깨져 있었음(실서버 curl로 재현·확인).
    각 조회 함수가 이제 embedding을 제거해 반환하는지 확인한다."""

    def setUp(self):
        self.test_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_no_blob_leak_test.db')
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        database.close_all_connections()
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.init_db()
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, category, target, "
                "support_detail, deadline, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("테스트사이트", "테스트 항목", "https://example.com/1", "복지", "일반",
                 "지원 내용", "2099-01-01", b'\x00\x01\x02\x03fake-embedding-bytes')
            )
            conn.commit()

    def tearDown(self):
        self.db_patcher.stop()
        database.close_all_connections()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def _assert_no_embedding_and_json_safe(self, items):
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertNotIn('embedding', item)
        json.dumps(items, ensure_ascii=False)  # bytes가 남아있으면 여기서 TypeError

    def test_load_data(self):
        self._assert_no_embedding_and_json_safe(history_manager.load_data(limit=10))

    def test_search_items(self):
        result = history_manager.search_items(query=None, limit=10)
        self._assert_no_embedding_and_json_safe(result["items"])

    def test_get_item_by_id(self):
        row = database.execute_query("SELECT id FROM welfare_items LIMIT 1", fetch_one=True)
        item = history_manager.get_item_by_id(row['id'])
        self.assertIsNotNone(item)
        self._assert_no_embedding_and_json_safe([item])

    def test_get_expiring_items(self):
        # deadline을 넉넉히 먼 미래로 넣었으니 기본 7일 창에는 안 걸림 — 큰 창으로 확인
        items = history_manager.get_expiring_items(days=365 * 100)
        self._assert_no_embedding_and_json_safe(items)


class TestGetItemDiff(unittest.TestCase):
    """(버그 2026-07-22, pyflakes로 발견) snapshots.py의 get_item_diff가 다른 모듈(items.py)의
    get_item_by_id를 import 없이 호출해 NameError가 났었다 — 넓은 except에 가려져 호출 시
    항상 조용히 None만 반환했음(테스트 커버리지가 없어 106건 통과에도 안 잡혔음). 지연 임포트로
    수정 후, 실제로 diff가 나오는지 확인한다."""

    def setUp(self):
        self.test_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_item_diff_test.db')
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        database.close_all_connections()
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.init_db()
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, category, target, "
                "support_detail, deadline) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("테스트사이트", "원본 제목", "https://example.com/diff", "복지", "일반",
                 "지원 내용", "2099-01-01")
            )
            conn.commit()
            item_id = conn.execute("SELECT id FROM welfare_items WHERE link = ?",
                                    ("https://example.com/diff",)).fetchone()['id']
            history_manager.snapshots._save_snapshot(conn.cursor(), "https://example.com/diff", "최초 저장")
            conn.commit()
            conn.execute("UPDATE welfare_items SET title = ? WHERE id = ?", ("바뀐 제목", item_id))
            conn.commit()
            self.item_id = item_id
            self.snapshot_id = conn.execute(
                "SELECT id FROM welfare_item_snapshots WHERE item_link = ?",
                ("https://example.com/diff",)).fetchone()['id']

    def tearDown(self):
        self.db_patcher.stop()
        database.close_all_connections()
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def test_get_item_diff_resolves_without_nameerror(self):
        diff = history_manager.get_item_diff(self.item_id, self.snapshot_id)
        self.assertIsNotNone(diff)


class TestValidateSite(unittest.TestCase):
    def test_valid_minimal(self):
        ok, _ = utils.validate_site({"name": "테스트", "url": "https://example.com"})
        self.assertTrue(ok)

    def test_missing_name(self):
        ok, reason = utils.validate_site({"url": "https://example.com"})
        self.assertFalse(ok)
        self.assertIn("name", reason)

    def test_missing_url(self):
        ok, reason = utils.validate_site({"name": "x"})
        self.assertFalse(ok)
        self.assertIn("url", reason)

    def test_bad_scheme(self):
        ok, reason = utils.validate_site({"name": "x", "url": "ftp://nope"})
        self.assertFalse(ok)
        self.assertIn("http", reason)

    def test_selectors_must_be_dict(self):
        ok, _ = utils.validate_site({"name": "x", "url": "https://a.com", "selectors": "bad"})
        self.assertFalse(ok)

    def test_max_pages_must_be_positive_int(self):
        ok, _ = utils.validate_site({"name": "x", "url": "https://a.com", "max_pages": 0})
        self.assertFalse(ok)
        ok, _ = utils.validate_site({"name": "x", "url": "https://a.com", "max_pages": "abc"})
        self.assertFalse(ok)
        ok, _ = utils.validate_site({"name": "x", "url": "https://a.com", "max_pages": 5})
        self.assertTrue(ok)

    def test_non_dict(self):
        ok, _ = utils.validate_site("not a dict")
        self.assertFalse(ok)


class TestNormalizeDate(unittest.TestCase):
    def test_iso_and_dotted(self):
        self.assertEqual(utils.normalize_date("2024-12-31"), "2024-12-31")
        self.assertEqual(utils.normalize_date("2024.1.5"), "2024-01-05")

    def test_korean_full(self):
        self.assertEqual(utils.normalize_date("2024년 3월 9일"), "2024-03-09")

    def test_deadline_tilde(self):
        self.assertEqual(utils.normalize_date("~2025.06.30"), "2025-06-30")

    def test_ongoing_returns_none(self):
        for s in ("상시모집", "연중 접수", "수시"):
            self.assertIsNone(utils.normalize_date(s))

    def test_empty(self):
        self.assertIsNone(utils.normalize_date(""))

    def test_invalid_date(self):
        self.assertIsNone(utils.normalize_date("2024-13-40"))


class TestExtractPageModifiedDate(unittest.TestCase):
    def test_basic(self):
        d = _extract_page_modified_date("문의 ... 최종수정일 : 2024-05-01 ...")
        self.assertEqual(d, datetime.date(2024, 5, 1))

    def test_korean_separators(self):
        d = _extract_page_modified_date("정보수정일 2023년 11월 2일")
        self.assertEqual(d, datetime.date(2023, 11, 2))

    def test_no_match(self):
        self.assertIsNone(_extract_page_modified_date("아무 날짜 없음"))

    def test_invalid_numbers(self):
        self.assertIsNone(_extract_page_modified_date("최종수정일 2024-99-99"))


class TestMergeLinkVision(unittest.TestCase):
    def setUp(self):
        self.engine = ScraperEngine()  # 브라우저 시작 없이 메서드만 사용
        self.cfg = {"name": "테스트사이트"}

    def test_vision_matches_link_uses_clean_title_and_href(self):
        link_items = [{"site_name": "테스트사이트", "title": "영유아 보육료 지원 안내문", "link": "https://a.com/1", "date": ""}]
        vision_items = [{"title": "영유아 보육료 지원", "date": "2024-01-01"}]
        merged = self.engine._merge_link_vision(link_items, vision_items, self.cfg)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["link"], "https://a.com/1")   # 링크 보존
        self.assertEqual(merged[0]["title"], "영유아 보육료 지원")  # Vision 제목 사용
        self.assertEqual(merged[0]["date"], "2024-01-01")        # 날짜 보강

    def test_no_match_added_without_link(self):
        link_items = [{"site_name": "테스트사이트", "title": "전혀 다른 공지사항입니다", "link": "https://a.com/9", "date": ""}]
        vision_items = [{"title": "아동수당 신규 신청", "date": ""}]
        merged = self.engine._merge_link_vision(link_items, vision_items, self.cfg)
        titles = {m["title"] for m in merged}
        self.assertIn("아동수당 신규 신청", titles)
        unmatched = [m for m in merged if m["title"] == "아동수당 신규 신청"][0]
        self.assertEqual(unmatched["link"], "")

    def test_empty_vision_returns_link_items(self):
        link_items = [{"site_name": "테스트사이트", "title": "공고", "link": "https://a.com/1", "date": ""}]
        merged = self.engine._merge_link_vision(link_items, [], self.cfg)
        self.assertEqual(merged, link_items)


class TestGeminiRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_allows_up_to_limit_immediately(self):
        rl = GeminiRateLimiter(max_calls=3, period=10.0)
        t0 = time.monotonic()
        for _ in range(3):
            await rl.acquire()
        self.assertLess(time.monotonic() - t0, 0.5)  # 한도 내 → 즉시

    async def test_blocks_when_exceeded(self):
        rl = GeminiRateLimiter(max_calls=2, period=0.6)
        await rl.acquire()
        await rl.acquire()
        t0 = time.monotonic()
        await rl.acquire()  # 3번째는 윈도우가 비워질 때까지 대기
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)


class TestEmbeddingService(unittest.TestCase):
    def setUp(self):
        from services import embedding_service as es
        self.es = es

    def test_blob_roundtrip(self):
        vec = [0.1, -0.2, 0.3, 0.4]
        blob = self.es.to_blob(vec)
        self.assertIsInstance(blob, bytes)
        restored = self.es.from_blob(blob)
        self.assertEqual(restored.size, len(vec))
        for a, b in zip(vec, restored.tolist()):
            self.assertAlmostEqual(a, b, places=5)

    def test_blob_none_safe(self):
        self.assertIsNone(self.es.to_blob(None))
        self.assertIsNone(self.es.from_blob(None))
        self.assertIsNone(self.es.from_blob(b''))

    def test_cosine_identical_and_orthogonal(self):
        import numpy as np
        a = np.asarray([1.0, 0.0, 0.0])
        self.assertAlmostEqual(self.es.cosine_sim(a, a), 1.0, places=5)
        self.assertAlmostEqual(self.es.cosine_sim(a, np.asarray([0.0, 1.0, 0.0])), 0.0, places=5)

    def test_rank_by_similarity_orders_and_filters(self):
        import numpy as np
        q = np.asarray([1.0, 0.0, 0.0], dtype='float32')
        cands = [
            ("near", np.asarray([0.95, 0.05, 0.0], dtype='float32')),
            ("far", np.asarray([0.0, 1.0, 0.0], dtype='float32')),
            ("mid", np.asarray([0.7, 0.7, 0.0], dtype='float32')),
        ]
        ranked = self.es.rank_by_similarity(q, cands, limit=3, min_score=0.0)
        self.assertEqual(ranked[0][0], "near")          # 가장 유사한 것이 먼저
        # min_score 필터: 0.5 이상만
        filtered = self.es.rank_by_similarity(q, cands, limit=3, min_score=0.5)
        labels = [p for p, _ in filtered]
        self.assertIn("near", labels)
        self.assertNotIn("far", labels)                 # 직교 → 제외

    def test_rank_empty_inputs(self):
        import numpy as np
        self.assertEqual(self.es.rank_by_similarity(None, [], limit=5), [])
        self.assertEqual(self.es.rank_by_similarity(np.asarray([1.0]), [], limit=5), [])

    def test_batch_empty_list_returns_empty(self):
        self.assertEqual(self.es.embed_texts_batch([]), [])

    def test_batch_no_client_returns_all_none(self):
        # (2026-07-28) 환경에 실제 GOOGLE_API_KEY가 있으면 client=None이 실제 _get_client()로
        # 폴백해 라이브 API를 호출해버렸다(오늘 실제 일일 한도 소진과 겹쳐 다른 테스트를
        # 오염시킨 것으로 발견). _get_client 자체를 강제로 None 반환하도록 패치해 환경과
        # 무관하게 "클라이언트 없음" 경로만 검증한다.
        orig = self.es._get_client
        self.es._get_client = lambda: None
        try:
            result = self.es.embed_texts_batch(["a", "b"], client=None)
            self.assertEqual(result, [None, None])
        finally:
            self.es._get_client = orig

    def test_batch_preserves_order_and_skips_empty_strings(self):
        # (2026-08-07, GPT Codex 리뷰로 발견) test_batch_no_client_returns_all_none과 같은
        # 종류의 오염 — 실제 GOOGLE_API_KEY가 있는 개발 환경에서 그날 실제 일일 임베딩
        # 한도가 소진돼 있으면(quota_state.json에 영속화됨) embed_texts_batch가 모킹된
        # client에 도달하기도 전에 조기 반환해버려, 이 테스트가 환경 상태에 암묵적으로
        # 의존하게 된다. _daily_quota_exhausted_today를 강제로 False로 고정해 격리한다.
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        emb1, emb2 = MagicMock(values=[0.1, 0.2]), MagicMock(values=[0.3, 0.4])
        mock_client.models.embed_content.return_value = MagicMock(embeddings=[emb1, emb2])

        orig = self.es._daily_quota_exhausted_today
        self.es._daily_quota_exhausted_today = lambda: False
        try:
            result = self.es.embed_texts_batch(["첫번째", "", "두번째"], client=mock_client)
        finally:
            self.es._daily_quota_exhausted_today = orig
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], [0.1, 0.2])
        self.assertIsNone(result[1], "빈 문자열은 API에 보내지 않고 None으로 남아야 한다")
        self.assertEqual(result[2], [0.3, 0.4])
        # API에는 빈 문자열을 제외한 2건만 전달되어야 한다
        call_kwargs = mock_client.models.embed_content.call_args.kwargs
        self.assertEqual(len(call_kwargs["contents"]), 2)

    def test_batch_exception_returns_all_none(self):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.models.embed_content.side_effect = RuntimeError("API 오류")
        result = self.es.embed_texts_batch(["a", "b", "c"], client=mock_client)
        self.assertEqual(result, [None, None, None])


class TestRelevanceFilter(unittest.TestCase):
    """지역(도봉구) + 연령(영유아~초등저학년) 정밀 필터."""

    def setUp(self):
        from services import relevance_filter
        self.rf = relevance_filter

    # ── 지역 ──
    def test_region_dobong(self):
        self.assertTrue(self.rf.region_ok({'residence': ['도봉구']}))
        self.assertTrue(self.rf.region_ok({'residence': ['서울시 도봉구 방학동']}))

    def test_region_seoul_citywide(self):
        self.assertTrue(self.rf.region_ok({'residence': ['서울시']}))
        self.assertTrue(self.rf.region_ok({'residence': ['전국']}))

    def test_region_other_district_excluded(self):
        self.assertFalse(self.rf.region_ok({'residence': ['노원구']}))
        self.assertFalse(self.rf.region_ok({'residence': ['서울시 강남구']}))

    def test_region_other_province_excluded(self):
        self.assertFalse(self.rf.region_ok({'residence': ['부산광역시']}))

    def test_region_unknown_passes(self):
        self.assertTrue(self.rf.region_ok({'residence': []}))
        self.assertTrue(self.rf.region_ok({}))

    def test_region_user_facing_examples(self):
        self.assertTrue(self.rf.region_ok({'residence': ['부산광역시 해운대구']}, '전국'))
        self.assertTrue(self.rf.region_ok({'residence': ['전국']}, '도봉구'))
        self.assertTrue(self.rf.region_ok({'residence': ['도봉구']}, '서울특별시'))
        self.assertTrue(self.rf.region_ok({'residence': ['서울시']}, '도봉구'))
        self.assertFalse(self.rf.region_ok({'residence': ['노원구']}, '도봉구'))
        self.assertFalse(self.rf.region_ok({'residence': ['부산광역시']}, '서울특별시'))

    # ── 연령 ──
    def test_age_infant_range(self):
        self.assertTrue(self.rf.age_ok({'start_age_months': 0, 'end_age_months': 71}))

    def test_age_overlaps_lower_elementary(self):
        # 0~12세 프로그램 → 겹침
        self.assertTrue(self.rf.age_ok({'start_age_months': 0, 'end_age_months': 144}))
        # 만 8세(96개월) 시작 → 상한 108 이내 → 통과
        self.assertTrue(self.rf.age_ok({'start_age_months': 96}))

    def test_age_upper_elementary_excluded(self):
        # 초등 4~6학년(만 10세~, 120개월~) → 상한(108) 초과 → 제외
        self.assertFalse(self.rf.age_ok({'start_age_months': 120}))

    def test_age_keyword_fallback(self):
        self.assertTrue(self.rf.age_ok({'target': '영유아'}))
        self.assertTrue(self.rf.age_ok({'target': '초등학생', 'age_detail': ''}))
        self.assertTrue(self.rf.age_ok({'life_cycle': ['유아', '학령기']}))

    def test_age_adult_excluded(self):
        self.assertFalse(self.rf.age_ok({'target': '노인', 'life_cycle': ['노년']}))
        self.assertFalse(self.rf.age_ok({'target': '청년'}))

    def test_age_unknown_passes(self):
        self.assertTrue(self.rf.age_ok({}))

    # ── 통합 ──
    def test_passes_post_ai_flags(self):
        ok, _ = self.rf.passes_post_ai({'residence': ['도봉구'], 'start_age_months': 12})
        self.assertTrue(ok)
        bad, reason = self.rf.passes_post_ai({'residence': ['부산'], 'start_age_months': 12})
        self.assertFalse(bad)
        self.assertIn('지역', reason)

    def test_widen_age_via_config(self):
        item = {'start_age_months': 120}  # 만 10세
        self.assertFalse(self.rf.age_ok(item, max_months=108))
        self.assertTrue(self.rf.age_ok(item, max_months=144))  # 상한 넓히면 통과


class TestCleanTitle(unittest.TestCase):
    """스크랩 제목의 브레드크럼(메뉴 경로) 제거."""

    def setUp(self):
        import scraper_engine
        self.ct = scraper_engine.clean_title
        self.nav = scraper_engine.is_nav_only_title

    def test_strip_breadcrumb_with_dash(self):
        t = "열린행정>알림마당>보도자료/해명 - 도봉구, 유아 녹색소비 체험 프로그램 운영, 참여자 모집(상세보기)"
        self.assertEqual(self.ct(t), "도봉구, 유아 녹색소비 체험 프로그램 운영, 참여자 모집")

    def test_strip_breadcrumb_last_segment(self):
        self.assertEqual(self.ct("보건사업>모자보건>미숙아·선천성이상아 의료비 지원"), "미숙아·선천성이상아 의료비 지원")

    def test_strip_site_colon_prefix(self):
        t = "서울특별시교육청 : 서울특별시교육청 > 교육정보 > 의무교육 > 공지사항"
        self.assertEqual(self.ct(t), "공지사항")

    def test_plain_title_unchanged(self):
        self.assertEqual(self.ct("2026년 아동수당 신청 안내"), "2026년 아동수당 신청 안내")

    def test_nav_only_detection(self):
        self.assertTrue(self.nav("공지사항"))
        self.assertTrue(self.nav(""))
        self.assertFalse(self.nav("도봉구 양육수당 안내"))


class TestDailyQuotaExhaustionPacificAndPersistence(unittest.TestCase):
    """(2026-08-05, GPT Codex 리뷰로 발견한 문제 2건의 회귀 테스트)
    ① 한국 로컬 날짜가 아니라 태평양 날짜로 판정해야 한다.
    ② 프로세스 재시작(모듈 재로드 시뮬레이션) 후에도 디스크에 저장된 상태를 유지해야 한다."""

    def setUp(self):
        from services import embedding_service as es
        self.es = es
        self._orig_date = es._daily_exhausted_date
        self._orig_loaded = es._state_loaded
        es._daily_exhausted_date = None
        es._state_loaded = False
        # 실제 앱 상태 파일을 건드리지 않도록 임시 경로로 격리
        import tempfile, os
        self._tmp_dir = tempfile.mkdtemp()
        self._patcher = patch.object(es, '_quota_state_path',
                                     return_value=os.path.join(self._tmp_dir, 'quota_state.json'))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.es._daily_exhausted_date = self._orig_date
        self.es._state_loaded = self._orig_loaded
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_uses_pacific_date_not_local(self):
        import datetime
        from zoneinfo import ZoneInfo
        pacific_today = datetime.datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        self.assertEqual(self.es._pacific_today_str(), pacific_today)

    def test_mark_exhausted_persists_across_simulated_restart(self):
        self.es._mark_daily_quota_exhausted()
        self.assertTrue(self.es._daily_quota_exhausted_today())

        # "재시작" 시뮬레이션: 메모리 캐시만 초기화 (디스크 상태 파일은 그대로 둠)
        self.es._daily_exhausted_date = None
        self.es._state_loaded = False

        self.assertTrue(self.es._daily_quota_exhausted_today(),
                        "재시작 후에도 디스크에 저장된 소진 상태를 다시 읽어야 한다")

    def test_stale_persisted_date_does_not_block(self):
        """어제(또는 그 이전) 날짜로 저장된 상태 파일은 오늘 판정에 영향을 주면 안 된다."""
        self.es._save_persisted_exhausted_date("2000-01-01")
        self.es._daily_exhausted_date = None
        self.es._state_loaded = False
        self.assertFalse(self.es._daily_quota_exhausted_today())


class TestQueryEmbeddingCache(unittest.TestCase):
    """질의 임베딩 LRU 캐시: 동일 검색어 반복 시 API 재호출을 막는지 검증."""

    def setUp(self):
        from services import embedding_service as es
        self.es = es
        es._query_cache.clear()
        self.calls = [0]

        class _FakeEmbeddings:
            def __init__(s):
                pass
            def embed_content(s, model, contents, config):
                self.calls[0] += 1
                class _E:
                    values = [0.1, 0.2, 0.3]
                class _R:
                    embeddings = [_E()]
                return _R()

        class _FakeClient:
            models = _FakeEmbeddings()

        self._orig = es._get_client
        es._get_client = lambda: _FakeClient()
        # (2026-08-07, GPT Codex 리뷰로 발견) 실제 개발 환경에 그날 일일 임베딩 한도 소진
        # 상태가 영속화돼 있으면(quota_state.json) embed_text가 이 가짜 client에 도달하기도
        # 전에 조기 반환해 캐시 호출 횟수 검증이 무의미해진다 — 강제로 False 고정.
        self._orig_quota = es._daily_quota_exhausted_today
        es._daily_quota_exhausted_today = lambda: False

    def tearDown(self):
        self.es._get_client = self._orig
        self.es._daily_quota_exhausted_today = self._orig_quota
        self.es._query_cache.clear()

    def test_query_cached_after_first_call(self):
        v1 = self.es.embed_text("청년 주거 지원", task_type='RETRIEVAL_QUERY')
        v2 = self.es.embed_text("청년 주거 지원", task_type='RETRIEVAL_QUERY')
        self.assertEqual(v1, v2)
        self.assertEqual(self.calls[0], 1)  # 두 번째는 캐시 → API 1회만

    def test_document_not_cached(self):
        self.es.embed_text("문서 내용", task_type='RETRIEVAL_DOCUMENT')
        self.es.embed_text("문서 내용", task_type='RETRIEVAL_DOCUMENT')
        self.assertEqual(self.calls[0], 2)  # 문서 임베딩은 캐싱 안 함


if __name__ == "__main__":
    unittest.main()

"""check_policies() 통합 테스트 — 기준선(baseline) 추출 실패 경로의 데이터 유실 방지.

(2026-08-05, GPT Codex 2차 리뷰로 발견) new_fields(1차 추출)는 성공했지만 last_extracted_fields가
없어서 시도하는 baseline_fields(2차, 이전 원문 추출)가 실패하는 경우, 예전 코드는 diff는 안
만들면서도 새 해시·last_extracted_fields를 확정해버려 그 변경을 영구히 놓쳤다. 실제 DB에
last_extracted_fields 누락이 10건 있어 이론적인 경우가 아니었다. 실제 임시 DB로
check_policies()를 두 번 연속 실행해(1회차 실패 → 2회차 재시도 성공) 검증한다.
"""
import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services import policy_sync as ps

_URL = "https://test.example/policy"
_PAGE_ID = "page-baseline-test"


class TestCheckPoliciesBaselineExtractionFailure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # database.run_db의 작업 스레드 연결이 Windows에서 테스트 종료 직후 잠깐 파일 핸들을
        # 유지할 수 있으므로, 격리는 고유 디렉터리로 보장하되 지연 정리 오류는 무시한다.
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db = os.path.join(self.tmp_dir.name, 'test_check_policies_baseline.db')
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
        self.policy_dbs_patcher = patch(
            'services.policy_sync.notion_service.get_policy_dbs',
            return_value=[{"key": "medical", "name": "의료·재활", "id": "test-db-id"}],
        )
        self.policy_dbs_patcher.start()
        database.close_all_connections()
        database.init_db()

        database.execute_write(
            "INSERT INTO notion_policies (db_key, page_id, title, notion_fields, source_urls, "
            "source_hashes, source_texts, last_extracted_fields) VALUES (?,?,?,?,?,?,?,?)",
            ("medical", _PAGE_ID, "테스트 정책", "{}",
             json.dumps([_URL]),
             json.dumps({_URL: "old_hash_abc"}),
             json.dumps({_URL: "예전 원문 내용입니다"}),
             None))  # last_extracted_fields 누락 상태(실측 10건과 동일 시나리오)

    def tearDown(self):
        database.close_all_connections()
        self.policy_dbs_patcher.stop()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.tmp_dir.cleanup()

    def _row(self):
        return database.execute_query(
            "SELECT source_hashes, source_texts, last_extracted_fields, change_status, pending_diff "
            "FROM notion_policies WHERE page_id=?", (_PAGE_ID,), fetch_one=True)

    async def _run_check(self, extract_side_effect):
        """_fetch_source(새 원문)와 ai_classifier.extract_policy_fields_async(추출)를 모킹해
        네트워크·실제 Gemini 호출 없이 check_policies()를 실행."""
        new_text = "새로 바뀐 원문 내용입니다 - 자격요건이 달라졌어요"

        async def fake_fetch_source(session, scraper, url, sem):
            return (new_text, new_text, False)

        with patch('services.policy_sync._fetch_source', side_effect=fake_fetch_source), \
             patch('ai_classifier.get_client', return_value=MagicMock()), \
             patch('ai_classifier.extract_policy_fields_async', side_effect=extract_side_effect):
            return await ps.check_policies(["medical"], scraper=object())

    async def test_baseline_extraction_failure_does_not_confirm_new_hash(self):
        """1차 추출 성공 + 2차(기준선) 추출 실패 → 해시·last_extracted_fields가 확정되면 안 된다."""
        async def flaky_extract(client, title, link, site_name, content=None, **_kwargs):
            if link.endswith("#prev"):
                return None  # extract_policy_fields_async는 실패 시 예외 없이 None을 반환(내부에서 catch)
            return {"category": "복지", "eligibility_detail": "새 자격요건"}

        result = await self._run_check(flaky_extract)
        self.assertEqual(result["changed"], 0, "기준선 실패로 이번엔 변경 후보가 생기면 안 된다")
        self.assertEqual(result["ai_required"], 2)
        self.assertEqual(result["ai_completed"], 1)
        self.assertEqual(result["ai_failed"], 1)

        row = self._row()
        stored_hashes = json.loads(row["source_hashes"])
        self.assertEqual(stored_hashes[_URL], "old_hash_abc",
                         "기준선 추출 실패 시 해시가 확정되면 안 된다(유실 버그 재현 방지)")
        self.assertIsNone(row["last_extracted_fields"],
                          "기준선 추출 실패 시 last_extracted_fields도 확정되면 안 된다")
        self.assertNotEqual(row["change_status"], "detected")

    async def test_retry_after_baseline_failure_detects_the_missed_change(self):
        """1회차(기준선 실패)로 유실 방지된 변경이, 2회차(기준선 성공) 재시도에서 실제로 잡히는지."""
        async def flaky_extract(client, title, link, site_name, content=None, **_kwargs):
            if link.endswith("#prev"):
                return None  # extract_policy_fields_async는 실패 시 예외 없이 None을 반환(내부에서 catch)
            return {"category": "복지", "eligibility_detail": "새 자격요건"}

        await self._run_check(flaky_extract)  # 1회차: 실패, 해시 되돌림 확인은 위 테스트에서 별도 검증

        async def ok_extract(client, title, link, site_name, content=None, **_kwargs):
            if link.endswith("#prev"):
                return {"category": "복지", "eligibility_detail": "예전 자격요건"}
            return {"category": "복지", "eligibility_detail": "새 자격요건"}

        result = await self._run_check(ok_extract)  # 2회차: 기준선 성공 → 이번엔 잡혀야 함

        row = self._row()
        self.assertIsNotNone(row["last_extracted_fields"], "재시도 성공 시엔 기준선이 확정돼야 한다")
        stored_hashes = json.loads(row["source_hashes"])
        self.assertNotEqual(stored_hashes[_URL], "old_hash_abc", "재시도 성공 시엔 해시도 갱신돼야 한다")
        # eligibility_detail 차이는 임베딩 비교 실패(mock client) 시 기본적으로 "high"로 판정되어
        # 변경 후보가 생성되어야 한다 — 유실됐던 변경이 재시도로 복구되는지가 이 테스트의 핵심.
        self.assertEqual(result["changed"], 1,
                         "1회차에 놓쳤던 변경이 2회차 재시도에서 실제로 잡혀야 한다")

    async def test_policy_ai_rpm_wait_is_counted_and_visible(self):
        """정책 점검 전용 RPM 제한이 두 번째 호출을 대기시키고 그 사실을 결과에 남긴다."""
        async def ok_extract(client, title, link, site_name, content=None, **_kwargs):
            # This fake represents a prepared provider attempt, not an input hold.
            await _kwargs['before_send']()
            if link.endswith("#prev"):
                return {"category": "복지", "eligibility_detail": "예전 자격요건"}
            return {"category": "복지", "eligibility_detail": "새 자격요건"}

        with patch.dict(os.environ, {
                "POLICY_AI_RPM": "1", "POLICY_AI_RATE_PERIOD": "0.02"}):
            result = await self._run_check(ok_extract)

        self.assertEqual(result["ai_required"], 2)
        self.assertEqual(result["ai_completed"], 2)
        self.assertEqual(result["ai_rate_waited"], 1)
        self.assertEqual(result["ai_failed"], 0)


class TestCheckPoliciesDoesNotReuseStaleGeneralCollectionCache(unittest.IsolatedAsyncioTestCase):
    """(2026-08-05, GPT Codex 3차 리뷰로 발견한 P0) check_policies의 Stage 2는 원문이
    실제로 바뀐 것을 Stage 1이 이미 확인한 뒤에만 실행되는데, 예전 코드는 그 URL이 과거
    일반 수집(welfare_items)으로 분석된 적 있으면 새 원문과 무관하게 그 캐시를 그대로
    재사용했다. 실제 운영 DB에서 정책 출처 URL과 일반 수집 캐시가 겹치는 사례(2건)가
    확인돼 이론적 케이스가 아니었다. Stage 2에서는 캐시를 쓰지 않고 항상 새로 추출해야
    한다 — 이 테스트는 캐시가 존재해도 AI가 실제로 다시 호출되고, 그 결과(캐시가 아닌
    새 값)가 저장되는지 검증한다."""

    def setUp(self):
        self.test_db = 'test_check_policies_stale_cache.db'
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
        self.policy_dbs_patcher = patch(
            'services.policy_sync.notion_service.get_policy_dbs',
            return_value=[{"key": "medical", "name": "의료·재활", "id": "test-db-id"}],
        )
        self.policy_dbs_patcher.start()
        database.close_all_connections()
        database.init_db()

        # 정책: last_extracted_fields를 미리 채워둬(기준선 문제와는 무관하게) 이 테스트가
        # 순수하게 "캐시 재사용 여부"만 검증하도록 격리.
        database.execute_write(
            "INSERT INTO notion_policies (db_key, page_id, title, notion_fields, source_urls, "
            "source_hashes, source_texts, last_extracted_fields) VALUES (?,?,?,?,?,?,?,?)",
            ("medical", _PAGE_ID, "겹침 테스트 정책", "{}",
             json.dumps([_URL]),
             json.dumps({_URL: "old_hash_xyz"}),
             json.dumps({_URL: "예전 원문"}),
             json.dumps({"category": "복지", "eligibility_detail": "예전 기준선"})))

        # 같은 URL(+겹치는 제목)로 일반 수집이 과거에 이미 AI 분석해둔 상태 — 겹침 재현.
        database.execute_write(
            "INSERT INTO welfare_items (site_name, title, link, ai_data, ai_call_at, scraped_at) "
            "VALUES (?,?,?,?,?,?)",
            ("테스트 사이트", "겹침 테스트 정책 안내", _URL,
             json.dumps({"category": "일반수집캐시_오염값", "eligibility_detail": "캐시된 옛날 값"}),
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"))

    def tearDown(self):
        database.close_all_connections()
        self.policy_dbs_patcher.stop()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    async def test_stage2_calls_ai_fresh_instead_of_reusing_general_collection_cache(self):
        new_text = "실제로 바뀐 새 원문 - 자격요건이 진짜 달라졌습니다"
        call_log = []

        async def fake_fetch_source(session, scraper, url, sem):
            return (new_text, new_text, False)

        async def fresh_extract(client, title, link, site_name, content=None, **_kwargs):
            call_log.append(link)
            return {"category": "복지", "eligibility_detail": "AI가 새로 분석한 값"}

        with patch('services.policy_sync._fetch_source', side_effect=fake_fetch_source), \
             patch('ai_classifier.get_client', return_value=MagicMock()), \
             patch('ai_classifier.extract_policy_fields_async', side_effect=fresh_extract):
            await ps.check_policies(["medical"], scraper=object())

        self.assertTrue(len(call_log) > 0,
                        "Stage 2는 캐시가 있어도 AI를 실제로 다시 호출해야 한다(캐시로 건너뛰면 안 됨)")

        row = database.execute_query(
            "SELECT last_extracted_fields FROM notion_policies WHERE page_id=?",
            (_PAGE_ID,), fetch_one=True)
        stored = json.loads(row["last_extracted_fields"])
        self.assertEqual(stored["eligibility_detail"], "AI가 새로 분석한 값",
                         "저장된 결과는 새로 호출한 AI 값이어야 하며, 일반 수집의 오염된 캐시값이면 안 된다")
        self.assertNotEqual(stored["eligibility_detail"], "캐시된 옛날 값")


class TestCheckPoliciesDedupsAlreadyStoredDuplicateSourceUrls(unittest.IsolatedAsyncioTestCase):
    """(2026-08-05, GPT Codex 4차 리뷰) extract_page_fields의 중복 제거는 다음 Notion
    동기화부터만 적용되므로, 이미 DB에 중복 저장된 기존 정책(실측: "희귀질환자 의료비
    지원")은 check_policies가 읽는 시점에도 방어적으로 중복 제거해야 한다. 같은 URL이
    바뀌었을 때 AI 입력(changed_text)에 그 원문이 두 번 병합되지 않는지 검증."""

    _DUP_URL = "https://test.example/dup-policy"
    _PAGE_ID2 = "page-dup-url-test"

    def setUp(self):
        self.test_db = 'test_check_policies_dup_url.db'
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
        self.policy_dbs_patcher = patch(
            'services.policy_sync.notion_service.get_policy_dbs',
            return_value=[{"key": "medical", "name": "의료·재활", "id": "test-db-id"}],
        )
        self.policy_dbs_patcher.start()
        database.close_all_connections()
        database.init_db()

        # source_urls에 같은 URL이 두 번 저장된 상태를 그대로 재현(마이그레이션 전 데이터).
        database.execute_write(
            "INSERT INTO notion_policies (db_key, page_id, title, notion_fields, source_urls, "
            "source_hashes, source_texts, last_extracted_fields) VALUES (?,?,?,?,?,?,?,?)",
            ("medical", self._PAGE_ID2, "중복 URL 테스트 정책", "{}",
             json.dumps([self._DUP_URL, self._DUP_URL]),
             json.dumps({self._DUP_URL: "old_hash"}),
             json.dumps({self._DUP_URL: "예전 원문"}),
             json.dumps({"category": "복지", "eligibility_detail": "예전 기준선"})))

    def tearDown(self):
        database.close_all_connections()
        self.policy_dbs_patcher.stop()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    async def test_ai_input_receives_changed_text_only_once(self):
        new_text = "실제로 바뀐 원문\n신청 조건\n딱 한 번만 들어가야 함"
        captured_content = []

        async def fake_fetch_source(session, scraper, url, sem):
            return (ps.policy_watch._normalize_text(new_text), new_text, False)

        async def capturing_extract(client, title, link, site_name, content=None, **_kwargs):
            if not link.endswith("#prev"):
                captured_content.append(content)
            return {"category": "복지", "eligibility_detail": "새 값"}

        with patch('services.policy_sync._fetch_source', side_effect=fake_fetch_source), \
             patch('ai_classifier.get_client', return_value=MagicMock()), \
             patch('ai_classifier.extract_policy_fields_async', side_effect=capturing_extract):
            await ps.check_policies(["medical"], scraper=object())

        self.assertTrue(len(captured_content) > 0)
        combined = captured_content[0] or ""
        # 원문이 중복 병합됐다면 "---"로 이어붙인 같은 텍스트가 두 번 나타난다.
        self.assertEqual(combined.count(new_text), 1,
                         f"중복 URL로 인해 원문이 두 번 병합되면 안 된다: {combined!r}")

    async def test_catalogue_structure_reaches_ai_and_survives_storage(self):
        import ai_classifier
        from unittest.mock import AsyncMock
        title = '공영주차장 주차 요금 감면'
        database.execute_write('UPDATE notion_policies SET title=? WHERE page_id=?', (title, self._PAGE_ID2))
        html = ('<ul><li>고궁 관람<ul><li>무료 입장</li></ul></li>'
                '<li>공영주차장 주차요금 감면<ul><li>등록장애인 동승 차량</li>'
                '<li>일반주차장 1시간 면제 후 50% 할인</li>'
                '<li>환승주차장 3시간 면제 후 80% 할인</li><li>' +
                '시험용 추가 안내입니다. ' * 30 +
                '</li></ul></li><li>고속도로 통행료<ul><li>별도 조건</li></ul></li></ul>')
        parsed = ai_classifier._parse_page_html(html, self._DUP_URL)
        captured = []

        async def extract(client, title, link, site_name, content=None, **kwargs):
            captured.append(content)
            return {'category': '복지', 'eligibility_detail': '예전 기준선'}

        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=parsed)) as fetch, \
             patch.object(ai_classifier, 'get_client', return_value=MagicMock()), \
             patch.object(ai_classifier, 'extract_policy_fields_async', side_effect=extract):
            await ps.check_policies(['medical'], scraper=object())
            self.assertEqual(fetch.await_count, 1, '같은 출처는 한 번만 수집')
            self.assertEqual(len(captured), 1)
            self.assertIn('등록장애인 동승 차량', captured[0])
            self.assertIn('환승주차장 3시간', captured[0])
            self.assertNotIn('고궁', captured[0])
            self.assertNotIn('고속도로', captured[0])
            row = database.execute_query('SELECT source_texts FROM notion_policies WHERE page_id=?',
                                         (self._PAGE_ID2,), fetch_one=True)
            self.assertEqual(json.loads(row['source_texts'])[self._DUP_URL], parsed['policy_text'])
            await ps.check_policies(['medical'], scraper=object())
            self.assertEqual(len(captured), 1)

    async def test_small_but_real_changes_are_not_skipped_by_similarity(self):
        import ai_classifier
        from unittest.mock import AsyncMock
        padding = '정책의 공통 안내 내용입니다. ' * 80
        for old, new in [('지원율 50%', '지원율 80%'), ('동승 가능', '동승 불가'),
                         ('신청 3월5일', '신청 5월3일')]:
            with self.subTest(change=new):
                before, after = padding + old, padding + new
                database.execute_write('UPDATE notion_policies SET source_hashes=?, source_texts=? WHERE page_id=?',
                    (json.dumps({self._DUP_URL: 'old_hash'}), json.dumps({self._DUP_URL: before}), self._PAGE_ID2))
                with patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(after, after, False))), \
                     patch.object(ai_classifier, 'get_client', return_value=MagicMock()), \
                     patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value={
                         'category': '복지', 'eligibility_detail': '예전 기준선'})) as ai:
                    await ps.check_policies(['medical'], scraper=object())
                    self.assertEqual(ai.await_count, 1)


if __name__ == '__main__':
    unittest.main()

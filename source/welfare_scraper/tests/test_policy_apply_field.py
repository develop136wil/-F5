"""policy_sync.apply_field_to_notion()의 remaining/completed 응답 검증.

(2026-08-07, GPT Codex 리뷰로 발견) 필드를 하나 적용해도 백엔드가 남은 필드 수를 안
돌려줘서, 프론트가 항상 "전체 완료"로 표시하던 버그의 회귀 방지 테스트. 실제 임시 DB를
사용한다(test_policy_sync_embed_batch.py와 동일 패턴).
"""
import asyncio
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services import policy_sync as ps
from services import notion_service


def live_page(title='테스트정책'):
    """정상 적용 테스트도 실제 최신 조회 성공을 전제로 한다."""
    values = {
        'title': title,
        'life_cycle': ['영유아'],
        'age_detail': '영유아',
        'eligibility_detail': '소득무관',
        'target_characteristics': ['일반'],
    }
    props = {}
    for key, name, kind in notion_service.get_policy_props():
        value = values.get(key)
        if kind == 'multi_select':
            props[name] = {kind: [{'name': v} for v in (value or [])]}
        elif kind in ('rich_text', 'title'):
            props[name] = {kind: [{'type':'text','text':{'content':value},'plain_text':value}] if value else []}
        else:
            props[name] = {kind: value}
        props[name].update(id=key, type=kind)
    return {'id': 'test-page', 'properties': props}


class TestApplyFieldRemainingCount(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # (2026-08-07) 테스트 메서드별 고유 파일명 — Windows에서 async DB 호출(database.run_db,
        # 스레드풀)이 커넥션을 즉시 안 놓아 같은 파일명을 공유하면 이전 테스트의 잔여 행이
        # 새는 문제를 test_ai_backlog.py에서 이미 발견·수정한 것과 동일한 패턴.
        self.test_db = f'test_policy_apply_field_{self._testMethodName}.db'
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

    def _insert_policy(self, fields):
        """pending_diff.fields에 field_label별 변경 항목을 넣은 정책 1건 생성."""
        for field in fields:
            field['snapshot_current'] = True
        pd = {"intensity": "high", "fields": fields, 'proposal_schema_version': 2,
              "extraction_meta": {"prompt_version": ps._EXTRACTION_PROMPT_VERSION}}
        database.execute_write(
            """INSERT INTO notion_policies (db_key, page_id, title, notion_fields,
               change_status, pending_diff)
               VALUES (?, ?, ?, ?, 'detected', ?)""",
            ('medical', 'page-1', '테스트정책', json.dumps({}, ensure_ascii=False),
             json.dumps(pd, ensure_ascii=False)))
        row = database.execute_query(
            "SELECT id FROM notion_policies WHERE page_id='page-1'", fetch_one=True)
        return row['id']

    async def test_acceptance_receipt_saved_with_local_mirror(self):
        label = ps._PROP_LABEL['support_detail']
        fields = [{'field': label, 'old': '기존', 'new': '50% 할인', 'level': 'high'},
                  {'field': ps._PROP_LABEL['age_detail'], 'old': '기존', 'new': '전체', 'level': 'high'}]
        policy_id = self._insert_policy(fields)
        database.execute_write('UPDATE notion_policies SET last_extracted_fields=? WHERE id=?',
                               (json.dumps({'support_detail': '50% 할인'}), policy_id))
        self.assertEqual(ps._finalize_apply(policy_id, label, {'fields': fields}, 'support_detail', '50% 감면'), 1)
        row = database.execute_query('SELECT notion_fields, pending_diff FROM notion_policies WHERE id=?',
                                     (policy_id,), fetch_one=True)
        pending = json.loads(row['pending_diff'])
        self.assertEqual(pending['fields'][0]['acceptance'],
                         {'key': 'support_detail', 'written': '50% 감면', 'extracted': '50% 할인'})
        self.assertEqual(json.loads(row['notion_fields'])['support_detail'], '50% 감면')

    async def test_old_or_unknown_extraction_stays_pending_without_live_access(self):
        label = ps._PROP_LABEL['support_detail']
        for meta in (None, {}, {'prompt_version': 'policy-extract-v1'},
                     {'prompt_version': 'policy-extract-v2-literal-conditions'}):
            with self.subTest(meta=meta):
                policy_id = self._insert_policy([
                    {'field': label, 'old': '기존 지원 내용', 'new': '월 10만원 지급', 'level': 'high'}])
                pd = {'fields': [{'field': label, 'old': '기존 지원 내용',
                                  'new': '월 10만원 지급', 'level': 'high'}]}
                if meta is not None:
                    pd['extraction_meta'] = meta
                saved = json.dumps(pd, ensure_ascii=False)
                database.execute_write('UPDATE notion_policies SET pending_diff=?, notion_fields=? WHERE id=?',
                    (saved, '{"support_detail":"기존 지원 내용"}', policy_id))
                with patch.object(notion_service, 'get_page') as read, \
                     patch.object(notion_service, 'update_page_properties') as write:
                    result = await ps._apply_field_to_notion_locked(policy_id, label, True, True)
                self.assertFalse(result['ok'])
                self.assertIn('이전 검증 버전', result['error'])
                read.assert_not_called()
                write.assert_not_called()
                row = database.execute_query('SELECT pending_diff, notion_fields FROM notion_policies WHERE id=?',
                    (policy_id,), fetch_one=True)
                self.assertEqual(row['pending_diff'], saved)
                self.assertEqual(json.loads(row['notion_fields'])['support_detail'], '기존 지원 내용')
                database.execute_write('DELETE FROM notion_policies WHERE id=?', (policy_id,))

    async def test_saved_source_criteria_hold_before_notion_even_with_both_forces(self):
        label = ps._PROP_LABEL['support_detail']
        for source in (
            '테스트정책\n지원내용: 교육비 지원\n신청 월에 만40세가 되는 자 포함',
            '테스트정책\n지원내용: 교육비 지원\n연령: 18~39세 (출생연도 기준: 1986.1.1.~2008.12.31.)',
            '테스트정책\n지원내용: 교육비 지원\n가구소득: 중위소득 50% 이하\n가입기준: 중위소득 100% 이하',
        ):
            with self.subTest(source=source):
                policy_id = self._insert_policy([
                    {'field': label, 'old': '기존 내용', 'new': '교육비 지원', 'level': 'high'}])
                sources = json.dumps({'https://example.invalid': source})
                database.execute_write('UPDATE notion_policies SET source_texts=? WHERE id=?', (sources, policy_id))
                before = dict(database.execute_query('SELECT * FROM notion_policies WHERE id=?',
                    (policy_id,), fetch_one=True))
                with patch.object(notion_service, 'get_page') as read, \
                     patch.object(notion_service, 'update_page_properties') as write:
                    result = await ps._apply_field_to_notion_locked(policy_id, label, True, True)
                self.assertFalse(result['ok'])
                self.assertIn('원문의 적용 조건', result['error'])
                read.assert_not_called()
                write.assert_not_called()
                after = dict(database.execute_query('SELECT * FROM notion_policies WHERE id=?',
                    (policy_id,), fetch_one=True))
                self.assertEqual(before, after)
                database.execute_write('DELETE FROM notion_policies WHERE id=?', (policy_id,))

    async def test_other_source_url_cannot_block_normal_proposal(self):
        label = ps._PROP_LABEL['support_detail']
        policy_id = self._insert_policy([
            {'field': label, 'old': '기존 내용', 'new': '교육비 지원', 'level': 'high'}])
        pd = json.loads(database.execute_query('SELECT pending_diff FROM notion_policies WHERE id=?',
            (policy_id,), fetch_one=True)['pending_diff'])
        pd['source_diff'] = {'url': 'https://example.invalid/selected', 'new': '교육비 지원'}
        sources = {'https://example.invalid/selected': '교육비 지원',
                   'https://example.invalid/unrelated': '신청 월에 만40세가 되는 자 포함'}
        database.execute_write('UPDATE notion_policies SET pending_diff=?, source_texts=? WHERE id=?',
            (json.dumps(pd), json.dumps(sources), policy_id))
        prepared, error, _ = ps._apply_field_sync(policy_id, label)
        self.assertIsNone(error)
        self.assertEqual(prepared['py_value'], '교육비 지원')

    async def test_invalid_proposals_never_reach_notion_even_with_force(self):
        for value in [None, '', 'None', ' null ', 'undefined', 'NaN', '-', '(없음)', [], {}]:
            with self.subTest(value=value):
                policy_id = self._insert_policy([
                    {"field": ps._PROP_LABEL['life_cycle'], "old": "영유아", "new": value, "level": "high"},
                ])
                with patch.object(notion_service, 'get_page') as read, patch.object(notion_service, 'update_page_properties') as write:
                    result = await ps._apply_field_to_notion_locked(
                        policy_id, ps._PROP_LABEL['life_cycle'], True, True)
                    self.assertFalse(result['ok'])
                    read.assert_not_called()
                    write.assert_not_called()
                database.execute_write('DELETE FROM notion_policies WHERE id=?', (policy_id,))

    async def test_unknown_sentinels_block_rich_text_and_select_before_live_read(self):
        # Tag clamping can already reject unknown lifecycle names. Check the
        # rich-text/select paths too, where a sentinel otherwise becomes data.
        for fkey in ('target_income', 'category'):
            for value in ('unknown', ' UNKNOWN ', '미상', ' 알 수 없음 ', '알수없음'):
                with self.subTest(fkey=fkey, value=value):
                    label = ps._PROP_LABEL[fkey]
                    policy_id = self._insert_policy([
                        {'field': label, 'old': '기존 정상값', 'new': value, 'level': 'high'},
                    ])
                    with patch.object(notion_service, 'get_page') as read, \
                         patch.object(notion_service, 'update_page_properties') as write:
                        result = await ps._apply_field_to_notion_locked(policy_id, label, True, True)
                    self.assertFalse(result['ok'])
                    self.assertIn('기존 Notion 내용은 유지', result['error'])
                    read.assert_not_called()
                    write.assert_not_called()
                    database.execute_write('DELETE FROM notion_policies WHERE id=?', (policy_id,))

    async def test_unknown_guard_does_not_match_policy_phrases_by_substring(self):
        label = ps._PROP_LABEL['target_income']
        for value in ('소득 무관', '소득 기준 없음', '가구원 소득을 알 수 없음으로 신고한 경우 별도 확인'):
            with self.subTest(value=value):
                policy_id = self._insert_policy([
                    {'field': label, 'old': '기존 정상값', 'new': value, 'level': 'high'},
                ])
                prepared, error, _pending = ps._apply_field_sync(policy_id, label)
                self.assertIsNone(error)
                self.assertEqual(prepared['py_value'], value)
                self.assertEqual(set(prepared['properties']), {'대상 소득 수준'})
                database.execute_write('DELETE FROM notion_policies WHERE id=?', (policy_id,))

    async def test_partial_apply_reports_remaining_and_keeps_card_visible(self):
        """3개 필드 중 1개만 적용 → remaining=2, completed=False, 카드는 여전히 detected."""
        policy_id = self._insert_policy([
            {"field": "대상연령", "old": "영유아", "new": "영유아, 초등학생", "level": "low"},
            {"field": "자격요건", "old": "소득무관", "new": "기준중위소득 100% 이하", "level": "low"},
            {"field": "지원대상특성", "old": "일반", "new": "일반, 장애/발달지연", "level": "low"},
        ])
        with patch.object(notion_service, 'get_page', return_value=live_page()), \
             patch.object(notion_service, 'update_page_properties', return_value={"ok": True}):
            res = await ps.apply_field_to_notion(policy_id, "대상연령", force=False)

        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("remaining"), 2)
        self.assertFalse(res.get("completed"))

        row = database.execute_query(
            "SELECT change_status FROM notion_policies WHERE id=?", (policy_id,), fetch_one=True)
        self.assertEqual(row["change_status"], "detected")  # 카드 계속 노출돼야 함

    async def test_last_field_apply_reports_completed_and_keeps_card(self):
        """마지막 필드 적용 뒤에도 명시적으로 정리할 때까지 카드를 유지한다."""
        policy_id = self._insert_policy([
            {"field": "자격요건", "old": "소득무관", "new": "소득무관 (전계층)", "level": "low"},
        ])
        with patch.object(notion_service, 'get_page', return_value=live_page()), \
             patch.object(notion_service, 'update_page_properties', return_value={"ok": True}):
            res = await ps.apply_field_to_notion(policy_id, "자격요건", force=False)

        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("remaining"), 0)
        self.assertTrue(res.get("completed"))

        row = database.execute_query(
            "SELECT change_status FROM notion_policies WHERE id=?", (policy_id,), fetch_one=True)
        self.assertEqual(row["change_status"], "detected")  # Explicit cleanup keeps undo accessible.

    async def test_local_finalize_is_atomic_and_surfaces_write_failure(self):
        policy_id = self._insert_policy([
            {"field": "자격요건", "old": "소득무관", "new": "기준중위소득 100% 이하", "level": "low"},
        ])
        with database.get_connection() as conn:
            conn.execute(
                """CREATE TRIGGER fail_policy_update BEFORE UPDATE ON notion_policies
                   BEGIN SELECT RAISE(ABORT, 'forced local save failure'); END""")
            conn.commit()
        pd = database.execute_query(
            "SELECT pending_diff FROM notion_policies WHERE id=?", (policy_id,), fetch_one=True)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'forced local save failure'):
            ps._finalize_apply(
                policy_id, "자격요건", json.loads(pd['pending_diff']),
                'eligibility_detail', '기준중위소득 100% 이하')
        snapshots = database.execute_query(
            "SELECT COUNT(*) AS n FROM welfare_item_snapshots WHERE item_link='notion:page-1'",
            fetch_one=True)
        self.assertEqual(snapshots['n'], 0, '상태 갱신 실패 시 먼저 쓴 스냅샷도 롤백되어야 합니다')


class TestConcurrentApplyDoesNotLoseAppliedFlag(unittest.IsolatedAsyncioTestCase):
    """(2026-08-28, GPT Codex 지적 ①) 같은 정책 카드의 서로 다른 필드를 거의 동시에 적용하면,
    각 요청이 서로 오래된 pending_diff를 읽어 나중에 저장한 쪽이 먼저 적용된 필드의
    applied=True 표시를 덮어쓸 수 있었다(Notion엔 둘 다 반영, 로컬 DB엔 하나만 반영된 것처럼
    남음). 정책별 락으로 이 경합을 직렬화해 막는 회귀 테스트."""

    def setUp(self):
        self.test_db = f'test_policy_apply_field_{self._testMethodName}.db'
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
        # (2026-08-28) _POLICY_APPLY_LOCKS는 policy_id(정수)로 키가 잡히는데, 매 테스트가
        # 자기만의 새 DB에서 다시 id=1부터 시작하다 보니 이전 테스트(다른 이벤트 루프)에서
        # 만든 Lock 객체를 그대로 재사용하게 돼 간헐적으로 깨졌다(단독 실행은 통과, 다른
        # 동시성 테스트와 같이 돌리면 실패 — 재현 확인). 테스트 간 완전히 격리한다.
        ps._POLICY_APPLY_LOCKS.clear()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        ps._POLICY_APPLY_LOCKS.clear()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def _insert_policy(self, fields):
        for field in fields:
            field['snapshot_current'] = True
        pd = {"intensity": "low", "fields": fields, 'proposal_schema_version': 2,
              "extraction_meta": {"prompt_version": ps._EXTRACTION_PROMPT_VERSION}}
        database.execute_write(
            """INSERT INTO notion_policies (db_key, page_id, title, notion_fields,
               change_status, pending_diff)
               VALUES (?, ?, ?, ?, 'detected', ?)""",
            ('medical', 'page-race', '동시적용테스트정책', json.dumps({}, ensure_ascii=False),
             json.dumps(pd, ensure_ascii=False)))
        row = database.execute_query(
            "SELECT id FROM notion_policies WHERE page_id='page-race'", fetch_one=True)
        return row['id']

    async def test_two_fields_applied_concurrently_both_persist(self):
        policy_id = self._insert_policy([
            {"field": "대상연령", "old": "영유아", "new": "영유아, 초등학생", "level": "low"},
            {"field": "자격요건", "old": "소득무관", "new": "소득무관 (전계층)", "level": "low"},
        ])

        async def _slow_ok(*args, **kwargs):
            # Notion API 왕복을 흉내낸 지연 — 락이 없다면 이 사이에 두 요청이 서로의
            # pending_diff 스냅샷을 들고 있다가 나중 저장이 먼저 저장을 덮어쓴다.
            await asyncio.sleep(0.05)
            return {"ok": True}

        with patch.object(notion_service, 'get_page', return_value=live_page('동시적용테스트정책')), \
             patch.object(notion_service, 'update_page_properties', side_effect=_slow_ok):
            r1, r2 = await asyncio.gather(
                ps.apply_field_to_notion(policy_id, "대상연령", force=False),
                ps.apply_field_to_notion(policy_id, "자격요건", force=False),
            )

        self.assertTrue(r1.get("ok"), r1)
        self.assertTrue(r2.get("ok"), r2)

        row = database.execute_query(
            "SELECT pending_diff, change_status FROM notion_policies WHERE id=?",
            (policy_id,), fetch_one=True)
        pd = json.loads(row["pending_diff"])
        applied_fields = {f["field"] for f in pd["fields"] if f.get("applied")}
        # 락이 없으면 둘 중 하나(먼저 적용된 쪽)의 applied 표시가 사라져 이 assert가 실패한다.
        self.assertEqual(applied_fields, {"대상연령", "자격요건"})
        self.assertEqual(row["change_status"], "detected")  # 완료 카드는 직접 정리할 때까지 유지.


class TestConcurrentApplyVsConfirmDoesNotResurrectDiff(unittest.IsolatedAsyncioTestCase):
    """(2026-08-28, GPT Codex 2차 리뷰) 필드끼리의 경합은 ①로 막았지만, "필드 적용 vs 처리완료"
    경합은 남아있었다 — set_status()가 정책별 락을 안 타서, 처리완료가 pending_diff=NULL로
    만든 직후 적용 작업이 자기가 예전에 읽은 pending_diff를 다시 저장해 되살릴 수 있었다.
    set_status_locked()로 같은 락을 쓰게 고친 뒤, 처리완료가 먼저 실행되면 뒤이은 적용
    요청이 (되살리지 않고) "이미 적용됨" 계열 오류로 안전하게 실패하는지 검증한다."""

    def setUp(self):
        self.test_db = f'test_policy_apply_field_{self._testMethodName}.db'
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
        # (2026-08-28) _POLICY_APPLY_LOCKS는 policy_id(정수)로 키가 잡히는데, 매 테스트가
        # 자기만의 새 DB에서 다시 id=1부터 시작하다 보니 이전 테스트(다른 이벤트 루프)에서
        # 만든 Lock 객체를 그대로 재사용하게 돼 간헐적으로 깨졌다(단독 실행은 통과, 다른
        # 동시성 테스트와 같이 돌리면 실패 — 재현 확인). 테스트 간 완전히 격리한다.
        ps._POLICY_APPLY_LOCKS.clear()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        ps._POLICY_APPLY_LOCKS.clear()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def _insert_policy(self, fields):
        for field in fields:
            field['snapshot_current'] = True
        pd = {"intensity": "low", "fields": fields, 'proposal_schema_version': 2,
              "extraction_meta": {"prompt_version": ps._EXTRACTION_PROMPT_VERSION}}
        database.execute_write(
            """INSERT INTO notion_policies (db_key, page_id, title, notion_fields,
               change_status, pending_diff)
               VALUES (?, ?, ?, ?, 'detected', ?)""",
            ('medical', 'page-confirm-race', '적용대처리완료경합테스트', json.dumps({}, ensure_ascii=False),
             json.dumps(pd, ensure_ascii=False)))
        row = database.execute_query(
            "SELECT id FROM notion_policies WHERE page_id='page-confirm-race'", fetch_one=True)
        return row['id']

    async def test_confirm_during_apply_is_serialized_not_corrupted(self):
        """정책에 필드 A·B 두 개가 남아있는데, A를 적용하는 도중(Notion PATCH 왕복 중) 사용자가
        카드 전체를 "처리 완료" 누르는 시나리오. asyncio.Event로 정확한 인터리빙을 강제해
        타이밍에 의존하지 않고 결정적으로 재현한다.

        락이 없으면: confirm이 즉시 끼어들어 pending_diff=NULL·change_status=confirmed로
        만드는데, 그 후 apply가 끝나면서 자기가 예전에 읽은 pd(A만 applied, B는 미적용)를
        되살려 저장 — 결과적으로 change_status=confirmed인데 pending_diff는 미적용 필드를
        여전히 들고 있는 모순 상태가 된다(카드가 완료 처리됐다는데 안 보이는 곳에 미해결
        데이터가 남음).

        락이 있으면: confirm이 apply의 락 해제를 기다렸다가 실행되므로, apply가 먼저
        pending_diff를 정상 갱신(A applied, B 남음)한 뒤 confirm이 그 위에 깔끔하게
        pending_diff=NULL·confirmed를 덮어써 모순이 생기지 않는다.
        """
        policy_id = self._insert_policy([
            {"field": "대상연령", "old": "영유아", "new": "영유아, 초등학생", "level": "low"},
            {"field": "자격요건", "old": "소득무관", "new": "기준중위소득 100% 이하", "level": "low"},
        ])

        started = asyncio.Event()
        proceed = asyncio.Event()

        async def _blocking_patch(*args, **kwargs):
            started.set()
            await proceed.wait()
            return {"ok": True}

        with patch.object(notion_service, 'get_page', return_value=live_page('적용대처리완료경합테스트')), \
             patch.object(notion_service, 'update_page_properties', side_effect=_blocking_patch):
            apply_task = asyncio.create_task(ps.apply_field_to_notion(policy_id, "대상연령", force=False))
            await asyncio.wait_for(started.wait(), timeout=5)  # 실패 시에도 테스트가 무한 대기하지 않음
            confirm_task = asyncio.create_task(ps.set_status_locked(policy_id, "confirmed"))
            # confirm이 락에 안 걸려있다면(버그) 이 사이에 먼저 끝나버릴 시간을 충분히 준다.
            await asyncio.sleep(0.05)
            proceed.set()
            apply_result = await apply_task
            await confirm_task

        self.assertTrue(apply_result.get("ok"), apply_result)

        row = database.execute_query(
            "SELECT pending_diff, change_status FROM notion_policies WHERE id=?",
            (policy_id,), fetch_one=True)
        # (2026-08-28, GPT Codex 재검토) confirm을 await했으므로 최종 상태는 반드시
        # confirmed여야 한다 — 두 조건을 독립적으로 단언해 향후 다른 회귀(예: 상태가
        # detected에 머무는 경우)까지 함께 잡는다.
        self.assertEqual(row["change_status"], "confirmed")
        # 핵심 검증: "confirmed인데 pending_diff가 미적용 필드를 들고 되살아나 있는" 모순 상태 금지.
        self.assertIsNone(row["pending_diff"],
                           "처리완료 상태인데 pending_diff가 되살아났습니다 — 락 없이 경합한 흔적")


if __name__ == '__main__':
    unittest.main()

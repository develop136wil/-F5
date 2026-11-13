"""(2026-08-28, GPT Codex 전수조사 ③+⑦) 정책 동기화·점검·통합점검·최초감사·재계산·
스타일재작성 + DB복원이 공유하는 유지보수 락(policy_sync._MAINTENANCE_LOCK) 검증.

배경: 예전엔 각 작업이 독립된 *_STATUS["running"] 플래그만 확인해 (a) run_full_check()가
sync_policies()/check_policies()를 직접 함수 호출해 그 엔드포인트 가드를 우회하고,
(b) "확인 후 백그라운드 등록" 사이에 await 지점이 있어 거의 동시 요청이 체크-후-설정
비원자성으로 둘 다 통과할 수 있었다. 이 테스트는 (1) 락 프리미티브 자체, (2) 서로 다른
작업(동기화 vs 통합점검) 간 실제 상호 배제, (3) DB 복원 검증 함수, (4) 유지보수 락이
걸려있을 때 DB 복원·필드 적용이 거부되는지를 확인한다.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException, UploadFile
from starlette.background import BackgroundTasks

import database
import api_server
from services import policy_sync as ps, ai_coordinator


def setUpModule():
    # Include indirect status/try_start_job reads, not only the endpoint that
    # acquires the bulk-job lock. Standalone tests use no operating quota state.
    temporary = tempfile.TemporaryDirectory(prefix='maintenance-ai-state-')
    unittest.addModuleCleanup(temporary.cleanup)
    for attribute, filename in (('_state_path', 'quota.json'), ('_job_lock_path', 'ai_job.lock')):
        isolated = patch.object(ai_coordinator, attribute, return_value=os.path.join(temporary.name, filename))
        isolated.start()
        unittest.addModuleCleanup(isolated.stop)
    ai_coordinator._reset_for_tests()
    unittest.addModuleCleanup(ai_coordinator._reset_for_tests)


class TestMaintenanceLockPrimitives(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ps.release_maintenance_lock()  # 이전 테스트가 비정상 종료해도 항상 풀린 상태로 시작

    def tearDown(self):
        ps.release_maintenance_lock()

    async def test_acquire_then_busy_then_release(self):
        self.assertIsNone(ps.maintenance_busy_message())
        ok1 = await ps.try_acquire_maintenance_lock("정책 동기화")
        self.assertTrue(ok1)
        ok2 = await ps.try_acquire_maintenance_lock("정책 점검")
        self.assertFalse(ok2)  # 이미 잠겨 있어 두 번째는 거부돼야 함
        msg = ps.maintenance_busy_message()
        self.assertIsNotNone(msg)
        self.assertIn("정책 동기화", msg)  # 무엇이 진행 중인지 사용자에게 보여야 함

        ps.release_maintenance_lock()
        self.assertIsNone(ps.maintenance_busy_message())
        ok3 = await ps.try_acquire_maintenance_lock("DB 복원")
        self.assertTrue(ok3)  # 해제 후엔 다시 잡을 수 있어야 함
        ps.release_maintenance_lock()


class TestMaintenanceLockAcrossEndpoints(unittest.IsolatedAsyncioTestCase):
    """(2026-08-28) run_full_check()가 내부적으로 sync_policies()/check_policies()를 직접
    호출해 엔드포인트 가드를 우회하던 문제 — 이제 서로 다른 정책 작업 엔드포인트끼리도
    공유 락으로 막히는지, 그리고 백그라운드 작업이 끝나면 락이 정상 해제되는지 확인한다."""

    def setUp(self):
        ps.release_maintenance_lock()
        self.ai_tmp = __import__('tempfile').TemporaryDirectory()
        self.ai_lock_patch = patch.object(
            ai_coordinator, '_job_lock_path',
            return_value=os.path.join(self.ai_tmp.name, 'ai_job.lock'),
        )
        self.ai_lock_patch.start()
        ai_coordinator._reset_for_tests()

    def tearDown(self):
        ps.release_maintenance_lock()
        ai_coordinator._reset_for_tests()
        self.ai_lock_patch.stop()
        self.ai_tmp.cleanup()

    async def test_sync_blocks_full_check_until_background_task_finishes(self):
        req = api_server.PolicyDbKeys(db_keys=[])

        with patch.object(ps, 'sync_policies', new=AsyncMock(return_value=None)):
            bg_sync = BackgroundTasks()
            res = await api_server.policy_sync_endpoint(req, bg_sync)
            self.assertEqual(res, {"status": "started"})

            # 동기화가 아직 "실행 중"(백그라운드 태스크를 우리가 일부러 안 돌림)인 상태 —
            # 이때 서로 다른 작업(통합점검)을 걸어도 같은 공유 락 때문에 막혀야 한다.
            bg_full = BackgroundTasks()
            with self.assertRaises(HTTPException) as ctx:
                await api_server.policy_full_check_endpoint(req, bg_full)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("정책 동기화", str(ctx.exception.detail))

            # 백그라운드 태스크(동기화)를 실제로 실행 — 끝나면 finally에서 락을 해제해야 한다.
            await bg_sync()

        self.assertIsNone(ps.maintenance_busy_message())  # 정상적으로 해제됐어야 함

        # 락이 풀렸으니 이제 통합점검은 통과해야 한다(백그라운드 태스크는 실행 안 시킴).
        with patch.object(ps, 'run_full_check', new=AsyncMock(return_value=None)):
            bg_full2 = BackgroundTasks()
            res2 = await api_server.policy_full_check_endpoint(req, bg_full2)
            self.assertEqual(res2, {"status": "started"})
            await bg_full2()  # 락 해제까지 확인

        self.assertIsNone(ps.maintenance_busy_message())

    async def test_field_apply_and_confirm_rejected_while_maintenance_running(self):
        """(GPT 권고) "동기화나 DB 복원 중에는 필드 적용도 차단하는 편이 안전" — 실제로
        차단되는지 확인. 정책은 DB에 존재할 필요조차 없다(락 단계에서 먼저 걸려야 함)."""
        ok = await ps.try_acquire_maintenance_lock("정책 동기화")
        self.assertTrue(ok)
        try:
            res = await ps.apply_field_to_notion(999999, "아무필드")
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("error"), "MAINTENANCE_BUSY")

            res2 = await ps.set_status_locked(999999, "confirmed")
            self.assertFalse(res2.get("ok"))
            self.assertEqual(res2.get("error"), "MAINTENANCE_BUSY")
        finally:
            ps.release_maintenance_lock()

    async def test_maintenance_rejected_after_field_apply_has_started(self):
        """필드 적용이 먼저 시작된 뒤에도 유지보수가 끼어들 수 없어야 한다."""
        import asyncio
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def _blocked_apply(*args, **kwargs):
            started.set()
            await proceed.wait()
            return {"ok": True, "remaining": 0, "completed": True}

        with patch.object(ps, '_apply_field_to_notion_locked', side_effect=_blocked_apply):
            task = asyncio.create_task(ps.apply_field_to_notion(1, "대상연령"))
            await started.wait()
            acquired = await ps.try_acquire_maintenance_lock("정책 동기화")
            self.assertFalse(acquired)
            self.assertIn("정책 필드 적용", ps.maintenance_busy_message())
            proceed.set()
            result = await task
        self.assertTrue(result.get("ok"))
        self.assertIsNone(ps.maintenance_busy_message())


class TestRestoreCandidateValidation(unittest.TestCase):
    """_validate_restore_candidate()가 실제 SQLite 파일을 검증하는지 (임시 파일로 실측)."""

    def setUp(self):
        self.tmp_paths = []

    def tearDown(self):
        for p in self.tmp_paths:
            if os.path.exists(p):
                os.remove(p)

    def _make_sqlite_file(self, tables):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp_paths.append(path)
        conn = sqlite3.connect(path)
        for t in tables:
            conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        return path

    def _make_schema_compatible_file(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp_paths.append(path)
        conn = sqlite3.connect(path)
        for table in sorted(api_server._RESTORE_REQUIRED_TABLES):
            required = api_server._RESTORE_REQUIRED_COLUMNS.get(table, {'id'})
            definitions = []
            for column in sorted(required):
                definitions.append(f'"{column}" INTEGER PRIMARY KEY' if column == 'id' else f'"{column}" TEXT')
            conn.execute(f'CREATE TABLE "{table}" ({", ".join(definitions)})')
        conn.commit()
        conn.close()
        return path

    def test_valid_db_with_required_tables_passes(self):
        path = self._make_schema_compatible_file()
        self.assertIsNone(api_server._validate_restore_candidate(path))

    def test_db_with_right_table_names_but_missing_columns_is_rejected(self):
        path = self._make_sqlite_file(sorted(api_server._RESTORE_REQUIRED_TABLES))
        reason = api_server._validate_restore_candidate(path)
        self.assertIsNotNone(reason)
        self.assertIn('필수 컬럼', reason)

    def test_db_missing_required_tables_is_rejected(self):
        """다른 프로그램의 정상 SQLite DB — magic byte는 통과하지만 이 앱의 테이블이 없다."""
        path = self._make_sqlite_file(['some_other_apps_table'])
        reason = api_server._validate_restore_candidate(path)
        self.assertIsNotNone(reason)
        self.assertIn('필수 테이블', reason)

    def test_non_sqlite_file_is_rejected(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.db')
        with os.fdopen(fd, 'wb') as f:
            f.write(b'not a real sqlite database at all, just garbage bytes')
        self.tmp_paths.append(path)
        reason = api_server._validate_restore_candidate(path)
        self.assertIsNotNone(reason)


class TestRestoreEndpointRespectsMaintenanceLock(unittest.IsolatedAsyncioTestCase):
    """DB 복원이 다른 정책 작업과 실제로 상호 배제되는지 — 실제 파일 검증 이전 단계에서
    거부되므로 업로드 내용은 아무 값이어도 된다."""

    def setUp(self):
        ps.release_maintenance_lock()

    def tearDown(self):
        ps.release_maintenance_lock()

    async def test_restore_rejected_while_another_policy_task_running(self):
        ok = await ps.try_acquire_maintenance_lock("정책 점검")
        self.assertTrue(ok)
        try:
            upload = UploadFile(file=io.BytesIO(b"SQLite format 3\x00"), filename="backup.db")
            with self.assertRaises(HTTPException) as ctx:
                await api_server.restore_database(file=upload)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("정책 점검", str(ctx.exception.detail))
        finally:
            ps.release_maintenance_lock()

    async def test_restore_rejected_while_scraper_running_and_lock_is_released(self):
        original = api_server.SCRAPER_STATUS["is_running"]
        api_server.SCRAPER_STATUS["is_running"] = True
        try:
            upload = UploadFile(file=io.BytesIO(b"SQLite format 3\x00"), filename="backup.db")
            with self.assertRaises(HTTPException) as ctx:
                await api_server.restore_database(file=upload)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIsNone(ps.maintenance_busy_message())
        finally:
            api_server.SCRAPER_STATUS["is_running"] = original


if __name__ == '__main__':
    unittest.main()

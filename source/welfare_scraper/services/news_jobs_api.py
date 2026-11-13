"""Job control only: these routes never launch collection or external AI calls."""
import asyncio
import sqlite3
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, StrictBool, ConfigDict

import database
from services import news_analysis_store as jobs
from services.news_profile_store import get_run_snapshot

router = APIRouter(prefix='/api/news', tags=['news-jobs'])


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_attempts: StrictInt = Field(ge=0)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: StrictBool


@router.get('/profiles/{profile_id}/jobs/{job_id}/import-preview')
async def import_preview(profile_id: str, job_id: str):
    from services import news_result_import
    return await _execute(lambda conn: news_result_import.preview(conn, profile_id, job_id))


@router.post('/profiles/{profile_id}/jobs/{job_id}/import')
async def import_result(profile_id: str, job_id: str, request: ImportRequest):
    from services import news_result_import, policy_sync, app_lifecycle
    from services.news_analysis_worker import _settle_db
    if not await policy_sync.acquire_db_mutation_lock('확인한 새 소식 가져오기'):
        raise HTTPException(409, policy_sync.maintenance_busy_message())
    try:
        if app_lifecycle.shutdown_requested:
            raise HTTPException(503, '프로그램이 종료 중입니다.')
        result, cancelled = await _settle_db(_execute(lambda conn: news_result_import.import_result(
            conn, profile_id, job_id, fingerprint=request.fingerprint, confirmed=request.confirmed)))
        if cancelled:
            raise asyncio.CancelledError()
        return {**result, 'ai_called': False, 'notion_uploaded': False}
    finally:
        policy_sync.release_maintenance_lock()


async def _execute(action):
    def work():
        with database.get_connection() as conn:
            return action(conn)
    try:
        return await database.run_db(work)
    except KeyError:
        raise HTTPException(404, '작업 또는 실행 기록을 찾을 수 없습니다.')
    except ValueError as error:
        # Never expose snapshot data, raw DB errors, or provider inputs.
        from services.news_result_import import ImportUnavailable
        if isinstance(error, ImportUnavailable) and error.reason == 'import_text_too_long':
            raise HTTPException(409, '본문이 길어 새 소식 후보로 가져오기를 보류했습니다. 분석 기록과 원문은 유지됩니다.')
        raise HTTPException(409, '작업 상태가 변경되었거나 요청을 처리할 수 없습니다. 다시 확인해주세요.')
    except database.DatabaseMaintenanceError:
        raise HTTPException(503, 'DB 유지보수 중입니다. 잠시 후 다시 시도해주세요.')
    except sqlite3.Error:
        raise HTTPException(503, '작업 기록을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.')


def _metadata(conn, job_id):
    job = jobs.get_job(conn, job_id)
    return {key: job[key] for key in ('job_id', 'run_id', 'state', 'attempts',
                                      'retry_after', 'reason', 'cancel_requested')}


@router.get('/runs/{run_id}/jobs')
async def list_jobs(run_id: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    def read(conn):
        get_run_snapshot(conn, run_id)
        return {'items': jobs.list_job_statuses(conn, run_id, limit=limit, offset=offset),
                'limit': limit, 'offset': offset}
    return await _execute(read)


@router.get('/jobs/{job_id}')
async def get_job(job_id: str):
    return await _execute(lambda conn: _metadata(conn, job_id))


@router.post('/jobs/{job_id}/cancel')
async def cancel_job(job_id: str):
    def cancel(conn):
        changed = jobs.cancel_job(conn, job_id)
        return {'changed': changed, 'job': _metadata(conn, job_id)}
    return await _execute(cancel)


@router.get('/profiles/{profile_id}/results')
async def profile_results(profile_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), run_id: str | None = Query(None, min_length=1, max_length=100)):
    return await _execute(lambda conn: jobs.list_profile_results(conn, profile_id, limit=limit, offset=offset, run_id=run_id))


@router.get('/profiles/{profile_id}/result-details')
async def profile_result_details(profile_id: str, record_id: str = Query(..., min_length=1, max_length=500)):
    return await _execute(lambda conn: jobs.get_profile_result_details(conn, profile_id, record_id))


@router.post('/jobs/{job_id}/resume')
async def resume_job(job_id: str, request: ResumeRequest):
    def resume(conn):
        jobs.resume_job(conn, job_id, expected_attempts=request.expected_attempts)
        return {'scheduled': False, 'job': _metadata(conn, job_id)}
    # queued means ready, NOT an automatic start. Scheduler integration is pending.
    return await _execute(resume)

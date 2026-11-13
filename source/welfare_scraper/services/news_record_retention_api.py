"""Explicit opt-in retention settings; never deletes saved news or Notion."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

import database
import utils
from services import news_record_retention as retention

router = APIRouter(prefix='/api/news/retention', tags=['news-retention'])
logger = logging.getLogger(__name__)


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    days: StrictInt
    confirmed: StrictBool


@router.get('')
async def get_settings():
    return {'days': retention.configured_days()}


def _save_and_cleanup(days):
    # Save before deleting: a failed configuration write must never delete data.
    utils.set_env_values({'NEWS_RECORD_RETENTION_DAYS': str(days)})
    if days == 0:
        return {'days': days, 'cleanup': None, 'cleanup_failed': False}
    try:
        with database.get_connection() as conn:
            result = retention.cleanup(conn, days)
        return {'days': days, 'cleanup': result, 'cleanup_failed': False}
    except Exception as error:
        logger.warning('새 소식 기록 보관 설정은 저장했으나 정리를 완료하지 못했습니다 (%s).', type(error).__name__)
        return {'days': days, 'cleanup': None, 'cleanup_failed': True}


@router.post('')
async def save_settings(request: RetentionRequest):
    from services import app_lifecycle, news_workflow_ownership, policy_sync
    from services.news_analysis_worker import _settle_db
    if request.days not in (0, 7, 30) or request.confirmed is not True:
        raise HTTPException(400, '보관 기간을 선택하고 정리 범위를 확인해주세요.')
    lease = news_workflow_ownership.acquire()
    if lease is None:
        raise HTTPException(409, '새 소식 수집이 끝난 뒤 보관 기간을 저장해주세요.')
    locked = False
    try:
        locked = await policy_sync.acquire_db_mutation_lock('새 소식 처리 기록 정리')
        if not locked:
            raise HTTPException(409, policy_sync.maintenance_busy_message())
        if app_lifecycle.shutdown_requested:
            raise HTTPException(503, '프로그램이 종료 중입니다.')
        result, cancelled = await _settle_db(database.run_db(_save_and_cleanup, request.days))
        if cancelled:
            raise asyncio.CancelledError()
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, '보관 기간을 저장하지 못했습니다. 기존 설정을 확인해주세요.') from None
    finally:
        if locked:
            policy_sync.release_maintenance_lock()
        lease.close()

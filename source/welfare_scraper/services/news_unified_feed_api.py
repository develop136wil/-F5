"""Local-only single-list projection and final-action preparation."""
import asyncio
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from services.news_jobs_api import _execute
from services import news_unified_feed as feed

router = APIRouter(prefix='/api/news/feed', tags=['news-feed'])


class FeedSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: StrictStr = Field(min_length=1, max_length=500)
    fingerprint: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')


class FeedPrepareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: StrictStr = Field(default='default-news', min_length=1, max_length=100)
    action: Literal['upload', 'html', 'dismiss', 'retry']
    items: list[FeedSelection] = Field(min_length=1, max_length=100)
    confirmed: StrictBool


@router.get('')
async def get_feed(profile_id: str = Query('default-news', min_length=1, max_length=100),
                   view: Literal['ready','review','processing','records'] = 'ready',
                   limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0)):
    from services import notion_service
    result = await _execute(lambda conn: feed.read_feed(conn, profile_id, view=view, limit=limit, offset=offset))
    return {**result, 'configured': notion_service.is_configured()}


@router.post('/prepare')
async def prepare_feed(request: FeedPrepareRequest):
    from services import app_lifecycle, policy_sync
    from services.news_analysis_worker import _settle_db
    if not await policy_sync.acquire_db_mutation_lock('선택한 새 소식 저장 준비'):
        raise HTTPException(409, policy_sync.maintenance_busy_message())
    try:
        if app_lifecycle.shutdown_requested:
            raise HTTPException(503, '프로그램이 종료 중입니다.')
        result, cancelled = await _settle_db(_execute(lambda conn: feed.prepare(
            conn, request.profile_id, [item.model_dump() for item in request.items],
            action=request.action, confirmed=request.confirmed)))
        if cancelled:
            raise asyncio.CancelledError()
        return result
    finally:
        policy_sync.release_maintenance_lock()

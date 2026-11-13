"""정책 watch 전용 모드 — 정해진 정책 페이지의 변경점만 집중 감지.

일반 수집(AI 분류, 비쌈)과 분리된 저비용 모니터링. 페이지 본문을 가져와(정적→렌더 폴백)
정규화 후 SHA-256 해시를 직전 값과 비교한다. 바뀌면 변경으로 기록(이전/현재 본문 보관 → diff).
AI 호출이 없어 빠르고 싸므로 자주 돌릴 수 있다. (도봉구·서울 영유아·임산부 정책 추적용)
"""
import re
import hashlib
import datetime
import database
from services.logger import setup_logger

logger = setup_logger(__name__)

# 매 로드마다 바뀌어 오탐을 유발하는 휘발성 패턴 제거 (수정일/마감일 등 '의미있는 날짜'는 보존)
_VOLATILE_PATTERNS = [
    re.compile(r'조회\s*:?\s*\d+'),        # 조회수
    re.compile(r'오늘\s*하루\s*열지\s*않기'),
    # A seconds-resolution timestamp may be an application deadline. Only
    # explicitly labelled live clocks are volatile, never arbitrary dates.
    re.compile(r'(?:현재\s*시각|접속\s*시각)\s*[:：]?\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}'),
]


def _normalize_text(text: str) -> str:
    """본문 정규화: 공백 정리 + 휘발성 토큰 제거 (안정적 비교용)."""
    if not text:
        return ''
    t = text
    for pat in _VOLATILE_PATTERNS:
        t = pat.sub('', t)
    return re.sub(r'\s+', ' ', t).strip()


def add_watch(name: str, url: str, scope: str = '') -> None:
    database.execute_write(
        "INSERT OR IGNORE INTO policy_watch (name, url, scope) VALUES (?, ?, ?)",
        (name, url, scope),
        raise_on_error=True,
    )


def list_watch() -> list:
    rows = database.execute_query(
        "SELECT id, name, url, scope, enabled, last_checked, last_changed_at, last_error, "
        "change_count, length(COALESCE(last_text,'')) AS text_len "
        "FROM policy_watch ORDER BY (last_changed_at IS NULL), last_changed_at DESC, id"
    )
    return [dict(r) for r in rows]


def remove_watch(watch_id: int) -> None:
    database.execute_write(
        "DELETE FROM policy_watch WHERE id = ?", (watch_id,), raise_on_error=True)


def set_enabled(watch_id: int, enabled: bool) -> None:
    database.execute_write(
        "UPDATE policy_watch SET enabled = ? WHERE id = ?", (1 if enabled else 0, watch_id),
        raise_on_error=True,
    )


def get_watch_diff(watch_id: int) -> dict:
    r = database.execute_query(
        "SELECT id, name, url, scope, prev_text, last_text, last_changed_at, change_count, last_error "
        "FROM policy_watch WHERE id = ?", (watch_id,), fetch_one=True)
    return dict(r) if r else None


async def run_watch_check(scraper=None) -> dict:
    """모든 활성 watch 페이지를 점검하고 변경을 기록. (검색/수집과 독립 실행)

    반환: {"checked": N, "changed": M, "items": [{id,name,changed,...}]}
    """
    rows = database.execute_query(
        "SELECT id, name, url, last_hash, last_text FROM policy_watch WHERE enabled = 1")
    if not rows:
        return {"checked": 0, "succeeded": 0, "failed": 0, "changed": 0,
                "status": "completed", "items": []}

    import aiohttp
    import asyncio
    from scraper_engine import ScraperEngine
    from services.policy_sync import _fetch_source, _relaxed_ssl_context

    own = scraper is None
    results = []
    changed = 0
    now = lambda: datetime.datetime.now().isoformat()
    try:
        if own:
            scraper = ScraperEngine(headless=True)
            await scraper.start()
        connector = aiohttp.TCPConnector(ssl=_relaxed_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            for r in rows:
                wid, name, url = r['id'], r['name'], r['url']
                last_hash, last_text = r['last_hash'], r['last_text']
                # Hash the verified page and attachment link manifest. This is
                # webpage monitoring, not certification of attachment contents.
                norm = ''
                error = ''
                try:
                    fetched = await _fetch_source(session, scraper, url, asyncio.Semaphore(1))
                    norm = fetched[0]
                    review = getattr(fetched, 'source_review', {})
                    # Links can be monitored without opening the attachment.
                    # Other coverage failures cannot become an unchanged page.
                    reasons = review.get('reasons', [])
                    blocking = [reason for reason in reasons if reason != 'attachment_review']
                    if blocking or (review and not reasons):
                        error = '페이지를 완전히 확인하지 못했습니다. 기존 비교 기준은 유지됩니다.'
                except Exception as e:
                    logger.warning(f"[정책 watch] fetch 실패 {url}: {e}")
                    error = '페이지를 읽지 못했습니다. 잠시 후 다시 확인해주세요.'

                if not norm or error:
                    error = error or '페이지 본문을 찾지 못했습니다. 출처 주소를 확인해주세요.'
                    database.execute_write(
                        "UPDATE policy_watch SET last_checked = ?, last_error = ? WHERE id = ?", (now(), error, wid),
                        raise_on_error=True)
                    results.append({"id": wid, "name": name, "changed": False,
                                    "error": error, "status": "error"})
                    continue

                new_hash = hashlib.sha256(norm.encode('utf-8')).hexdigest()
                if last_hash is None:
                    # 최초 점검 → 기준선만 저장 (변경 아님)
                    database.execute_write(
                        "UPDATE policy_watch SET last_hash=?, last_text=?, last_checked=?, last_error=NULL WHERE id=?",
                        (new_hash, norm, now(), wid), raise_on_error=True)
                    results.append({"id": wid, "name": name, "changed": False, "baseline": True})
                elif new_hash != last_hash:
                    ts = now()
                    database.execute_write(
                        "UPDATE policy_watch SET prev_text=?, last_text=?, last_hash=?, "
                        "last_checked=?, last_changed_at=?, last_error=NULL, change_count=change_count+1 WHERE id=?",
                        (last_text, norm, new_hash, ts, ts, wid), raise_on_error=True)
                    changed += 1
                    results.append({"id": wid, "name": name, "changed": True})
                    logger.info(f"🔔 [정책 watch] 변경 감지: {name} ({url})")
                else:
                    database.execute_write(
                        "UPDATE policy_watch SET last_checked = ?, last_error = NULL WHERE id = ?", (now(), wid),
                        raise_on_error=True)
                    results.append({"id": wid, "name": name, "changed": False})
    except Exception:
        # Includes browser startup failure: every unprocessed page remains an
        # explicit failure and a future run can retry its untouched baseline.
        processed = {item['id'] for item in results}
        error = '점검을 완료하지 못했습니다. 잠시 후 다시 확인해주세요.'
        for row in rows:
            if row['id'] in processed:
                continue
            database.execute_write(
                'UPDATE policy_watch SET last_checked=?, last_error=? WHERE id=?',
                (now(), error, row['id']), raise_on_error=True)
            results.append({'id': row['id'], 'name': row['name'], 'changed': False,
                            'error': error, 'status': 'error'})
    finally:
        if own and scraper is not None:
            try:
                await scraper.close()
            except Exception:
                logger.warning('[정책 watch] 브라우저 정리 실패')

    failed = sum(bool(item.get('error')) for item in results)
    logger.info(f"🔍 [정책 watch] 점검 {len(rows)}건 · 변경 {changed}건 · 확인 실패 {failed}건")
    return {"checked": len(rows), "succeeded": len(rows) - failed, "failed": failed,
            "changed": changed, "status": ('error' if failed == len(rows) else 'partial') if failed else 'completed',
            "items": results}

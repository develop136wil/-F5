import asyncio
import datetime
import os
import re
import time
import aiohttp
from services.logger import setup_logger
from scraper_engine import ScraperEngine
import content_filter
import history_manager
import database
from services import relevance_filter, news_attachments, legacy_news_analysis
import notifier
import ai_classifier
import utils
import json
import hashlib

logger = setup_logger("ScraperService")

# 페이지 최종수정일 추출 정규식 (최종수정일, 수정일시, 업데이트일 등 한국 공공기관 공통 표현)
_PAGE_MOD_RE = re.compile(
    r'(?:최종\s*수정일|최종\s*업데이트|수정\s*일시?|업데이트\s*일시?|갱신\s*일시?|정보수정일)'
    r'\s*[:\s]*(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})',
    re.IGNORECASE
)

def _extract_page_modified_date(text: str):
    """페이지 텍스트에서 최종수정일을 추출. datetime.date 또는 None 반환."""
    if not text:
        return None
    m = _PAGE_MOD_RE.search(text)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except (ValueError, TypeError):
        return None


def _is_past_deadline(item: dict) -> bool:
    """항목의 마감일(deadline) 또는 행사 종료일(event_end_date)이 오늘 이전이면 True.

    날짜가 없거나 파싱 불가하면 상시 모집으로 간주(False). 마감 지난 공고 수집 차단용.
    """
    today = datetime.datetime.now().date()
    for val in (item.get('deadline'), item.get('event_end_date')):
        if val:
            try:
                parsed = datetime.datetime.strptime(str(val)[:10], '%Y-%m-%d').date()
                if parsed < today:
                    return True
            except (ValueError, TypeError):
                pass  # 파싱 실패 시 상시로 간주
    # 날짜 필드가 없어도, 제목·날짜필드의 연도가 모두 과거면 지난 공고로 간주
    # (예: '2025년 발달장애인가족 휴식지원...' → 2026년엔 마감). 현재/미래 연도가 있으면 통과.
    blob = "{} {} {} {}".format(item.get('title', '') or '', item.get('event_date', '') or '',
                                item.get('deadline', '') or '', item.get('event_end_date', '') or '')
    years = [int(y) for y in re.findall(r'(20\d\d)', blob)]
    if years and max(years) < today.year:
        return True
    return False


# ── 교차 프로세스 스크래핑 락 ─────────────────────────────────────────────────
# 인앱 예약(APScheduler)·수동 실행은 같은 프로세스라 _scraper_lock으로 보호되지만,
# OS 작업 스케줄러(--run-once)는 별도 프로세스라 동시 실행될 수 있다.
# AppData의 lock 파일에 OS 배타 락을 걸어 프로세스 간 중복 수집을 방지한다.
def _acquire_scrape_lock():
    """스크래핑 OS 파일 락 획득. 성공 시 파일 핸들, 실패(이미 실행 중) 시 None."""
    try:
        import utils as _utils
        lock_path = os.path.join(_utils.get_app_data_dir(), 'scraper.lock')
    except Exception:
        lock_path = os.path.join(os.path.expanduser('~'), '.searchmaster_scraper.lock')
    try:
        f = open(lock_path, 'a+')
        f.seek(0)  # 모든 프로세스가 같은 바이트를 잠가야 한다.
    except Exception:
        return None
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        try:
            f.close()
        except Exception:
            pass
        return None


def _release_scrape_lock(handle):
    """_acquire_scrape_lock으로 얻은 락 해제."""
    if handle is None:
        return
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        try:
            handle.close()
        except Exception:
            pass


class GeminiRateLimiter:
    """
    Gemini Free Tier 슬라이딩 윈도우 Rate Limiter.

    분당 max_calls 건 이하로 Gemini API 호출을 제한한다.
    호출자가 모델별 실측 한도에 맞춰 max_calls를 지정한다
    (2026-07-24 AI Studio 실측: gemini-3.5-flash-lite 15 RPM, gemini-embedding-001 100 RPM).

    동작 방식:
      - 최근 60초 내 호출 횟수가 max_calls 미만이면 즉시 허용.
      - 한도 도달 시 가장 오래된 호출이 윈도우를 벗어날 때까지 대기.
    """

    def __init__(self, max_calls: int = 4, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._lock = asyncio.Lock()
        self._call_times: list = []
        self._last_wait_log = 0.0  # 대기 로그 스로틀용(마지막 로그 시각)

    async def acquire(self):
        """Rate limit 범위 내에서 요청을 허용. 초과 시 자동 대기.

        Lock을 보유한 채 sleep하면 다른 모든 대기자를 차단하므로,
        대기 시간 계산 → Lock 해제 → sleep → Lock 재획득 → 기록 추가 순서로 처리한다.
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                self._call_times = [t for t in self._call_times if now - t < self.period]

                if len(self._call_times) < self.max_calls:
                    self._call_times.append(now)
                    return  # 허용 — Lock 해제 후 반환

                # 한도 도달: 대기 시간 계산 후 Lock 해제
                wait_time = self.period - (now - self._call_times[0]) + 0.5

            # Lock 밖에서 sleep — 다른 acquire() 호출자가 Lock을 획득할 수 있음
            if wait_time > 0:
                # (2026-07-24) 여러 항목이 동시에 한도에 걸리면 대기자마다 이 줄을 찍어
                # 같은 순간 수십 줄이 몰려 로그가 폭주했다(0.1초에 여러 줄). 성능 문제는
                # 아니지만 로그가 지저분하고 불안해 보여, 리미터당 5초에 한 줄로 스로틀한다.
                if now - self._last_wait_log > 5.0:
                    self._last_wait_log = now
                    logger.info(
                        f"[Rate Limiter] Gemini 분당 {self.max_calls}건 한도 도달 — "
                        f"약 {wait_time:.0f}초 대기 후 재개 (대기 중 항목은 순차 처리됩니다)"
                    )
                await asyncio.sleep(wait_time)
            # sleep 후 루프 재진입하여 조건 재확인


# Simple retry decorator or logic
async def retry_async(func, *args, retries=3, delay=1, **kwargs):
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == retries - 1:
                raise e
            logger.warning(f"Retry {attempt+1}/{retries} for {func.__name__} due to: {e}")
            await asyncio.sleep(delay * (2 ** attempt)) # Exponential backoff


async def _cancellable_gather(tasks: list, stop_event: asyncio.Event | None, cancel_msg: str = "🛑 Stopped by user."):
    """
    stop_event가 있을 때 취소 가능한 asyncio.gather 래퍼.
    stop_event가 먼저 set되면 pending task를 취소하고 None 반환.
    완료되면 결과 리스트를 반환.
    """
    if not stop_event:
        return await asyncio.gather(*tasks, return_exceptions=True)

    stop_task = asyncio.create_task(stop_event.wait())
    main_task = asyncio.gather(*tasks, return_exceptions=True)

    done, pending = await asyncio.wait([main_task, stop_task], return_when=asyncio.FIRST_COMPLETED)

    if stop_task in done:
        logger.info(cancel_msg)
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        return None  # 취소 신호

    # main_task가 먼저 완료 — stop_task가 pending에 남아 있으면 취소하여 Task 누수 방지
    stop_task.cancel()
    try:
        await stop_task
    except asyncio.CancelledError:
        pass
    return await main_task


async def _cancellable_as_completed(tasks: list, stop_event: asyncio.Event | None):
    """
    (2026-07-28) asyncio.gather 기반 _cancellable_gather는 전체 태스크가 끝날 때까지
    기다렸다가 결과를 일괄 반환하므로, 호출부가 그 뒤에야 진행률을 갱신하면 "0%에
    오래 멈췄다가 한번에 점프"하는 표시 버그가 생긴다(라이브 시연에서 실제로 관찰됨).
    이 함수는 완료되는 태스크를 즉시 (index, result_or_exception)으로 하나씩 넘겨줘
    호출부가 사이트별 완료 시점에 바로 진행률을 갱신할 수 있게 한다.
    stop_event가 set되면 남은 태스크를 취소하고 조용히 반복을 끝낸다(예외를 던지지 않음) —
    호출부는 루프 종료 후 stop_event.is_set()으로 취소 여부를 판단해야 한다.
    """
    task_map = {asyncio.ensure_future(t): i for i, t in enumerate(tasks)}
    pending = set(task_map.keys())
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event else None

    try:
        while pending:
            wait_set = pending | ({stop_task} if stop_task else set())
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if stop_task and stop_task in done:
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                return

            for t in done:
                pending.discard(t)
                idx = task_map[t]
                try:
                    result = t.result()
                except Exception as e:
                    result = e
                yield idx, result
    finally:
        if stop_task and not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass


class ScraperService:
    def __init__(self, headless=True):
        self.headless = headless
        # (2026-07-27 재조정, GPT Codex 리뷰로 발견한 문제 반영) 분류·OCR·Vision은 같은 모델
        # (기본 gemini-3.5-flash-lite)을 쓰므로 하나의 RPM 예산(무료 등급 실측 15 RPM)을
        # 나눠 쓴다: 분류 6 + OCR 3 + Vision 3(scraper_engine._vision_rate_limiter) = 12/분,
        # 3건 여유. 이전 값(8+4=12)은 Vision 호출이 리미터를 아예 안 거치던 버그를 놓치고
        # "분류+OCR만 12"로 계산해, Vision까지 겹치면 실제 부하가 15를 넘겨 429가 반복
        # 발생했다(실측 로그: capture_page_vision 429, 동시 다발 재시도). 세 리미터가 별개
        # 객체라 이론상 최악의 경우 여전히 겹칠 수 있지만, 각 몫을 줄여 그 여지를 좁힌다.
        # 완전한 단일 전역 대기열 통합은 더 큰 리팩터링이라 제출 후 과제로 남긴다.
        _rate_calls = int(os.getenv('AI_RATE_MAX_CALLS', '6'))
        _rate_period = float(os.getenv('AI_RATE_PERIOD_SEC', '60.0'))
        self.ai_rate_limiter = GeminiRateLimiter(max_calls=_rate_calls, period=_rate_period)
        # OCR Rate Limiter: 분류·Vision과 같은 모델·같은 RPM 예산을 공유 (위 주석 참조)
        _ocr_calls = int(os.getenv('OCR_RATE_MAX_CALLS', '3'))
        self.ocr_rate_limiter = GeminiRateLimiter(max_calls=_ocr_calls, period=_rate_period)
        # 임베딩 Rate Limiter: gemini-embedding-001 (별도 쿼터, 무료 등급 실측 100 RPM)
        # 환경변수: EMBED_RATE_MAX_CALLS (기본 60), AI_RATE_PERIOD_SEC 공유
        _embed_calls = int(os.getenv('EMBED_RATE_MAX_CALLS', '60'))
        self.embed_rate_limiter = GeminiRateLimiter(max_calls=_embed_calls, period=_rate_period)
        # 동시 스크래핑 수 제한: 브라우저 Playwright context 폭발 방지
        self.scrape_semaphore = asyncio.Semaphore(
            int(os.getenv('MAX_CONCURRENT_SCRAPES', '3'))
        )
        # 마지막 실행의 AI 절감 통계 (UI 노출용)
        self.last_ai_savings = {'cache': 0, 'semantic': 0, 'quota_skipped': 0, 'analyzed': 0, 'saved_total': 0,
                                 'backlog_completed': 0, 'backlog_deferred': 0, 'backlog_permanent': 0,
                                 'analysis_failed': 0, 'analysis_deferred': 0}
        self.last_save_result = {'requested': 0, 'saved': 0, 'processed': 0, 'skipped': 0}
        # Run-once opts into status-bearing engine responses. The public return
        # value and normal GUI scrape behavior remain the legacy list contract.
        self.track_collection_outcome = True
        self.last_run_outcome = None

    async def run_scraper_async(self, progress_callback=None, stop_event=None):
        """
        Runs the full scraping process asynchronously.
        """
        logger.info("🚀 Starting Async Scraper Service...")
        self.last_run_outcome = {'state': 'running', 'sites_processed': 0, 'sites_failed': 0}
        self.last_ai_savings = {key: 0 for key in self.last_ai_savings}
        self.last_save_result = {'requested': 0, 'saved': 0, 'processed': 0, 'skipped': 0}
        
        # 1. Load Configuration
        sites = utils.load_sites()
        if not sites:
            logger.error("No sites found.")
            self.last_run_outcome['state'] = 'not_ready'
            return []

        # 1-b. Incremental Scraping: SCRAPE_INTERVAL_HOURS 환경변수가 설정된 경우
        #       마지막 스크래핑으로부터 해당 시간이 지나지 않은 사이트는 건너뜀.
        #       기본값 0 = 비활성화 (항상 전체 스크래핑).
        interval_hours = float(os.getenv('SCRAPE_INTERVAL_HOURS', '0'))
        if interval_hours > 0:
            cutoff = datetime.datetime.now() - datetime.timedelta(hours=interval_hours)
            active_sites, skipped_names = [], []
            # 사이트별 마지막 스크래핑 시각을 한 번에 조회 (N+1 DB 호출 방지)
            last_map = await database.run_db(
                history_manager.get_sites_last_scraped, [s['name'] for s in sites]
            )
            for site in sites:
                last = last_map.get(site['name'])
                if last and last > cutoff:
                    skipped_names.append(site['name'])
                else:
                    active_sites.append(site)
            if skipped_names:
                preview = ', '.join(skipped_names[:5])
                tail = f' 외 {len(skipped_names)-5}개' if len(skipped_names) > 5 else ''
                logger.info(
                    f"⏭️ [증분 스크래핑] {len(skipped_names)}개 건너뜀 "
                    f"(최근 {interval_hours}h 내 완료): {preview}{tail}"
                )
            sites = active_sites
            if not sites:
                logger.info("ℹ️ 모든 사이트가 최근에 스크래핑되어 이번 회차는 생략합니다.")
                self.last_run_outcome['state'] = 'completed'
                return []

        # 2. Load History
        history = await database.run_db(history_manager.load_history)
        logger.info(f"📚 Loaded history: {len(history)} items")
        # set 변환: scrape_site 조기 종료용 O(1) 조회
        known_links: set = set(history)

        # Initialize AI Client
        ai_client = ai_classifier.get_client()
        if not ai_client:
            logger.warning("⚠️ Google API Key missing. AI features will be disabled.")

        # 교차 프로세스 중복 실행 방지 (OS 작업 vs 앱 동시 실행)
        lock_handle = await asyncio.to_thread(_acquire_scrape_lock)
        if lock_handle is None:
            logger.warning("⚠️ 다른 프로세스에서 스크래핑이 이미 실행 중입니다 — 이번 실행을 건너뜁니다.")
            self.last_run_outcome['state'] = 'busy'
            return []

        scraper = ScraperEngine(headless=self.headless)

        new_items = []
        pending_items = []

        try:
            # (UX 2026-07-24) 브라우저 엔진 실행(특히 최초 실행 시 Chromium 자동 설치)은 수 초~수 분
            # 걸리는데, 그동안 진행률이 고정돼 있어 멈춘 것처럼 보인다. 실행 직전에 안내 문구를 띄워
            # "로딩이 안 되는 게 아니라 준비 중"임을 알린다.
            if progress_callback:
                progress_callback(0, len(sites), "브라우저 엔진을 준비하는 중이에요… (처음 실행할 땐 엔진 설치로 시간이 더 걸릴 수 있어요)")

            # (버그 2026-07-23) start()가 try 밖에 있으면 브라우저 실행 실패 시 finally가
            # 돌지 않아 이미 시작된 Playwright 드라이버 프로세스가 누수되고, 위에서 잡은
            # scraper.lock도 명시 해제되지 않아(GC 의존) 다음 수집이 계속 막힐 수 있다.
            # try 안으로 옮겨 어떤 실패든 finally에서 close()+락 해제가 보장되게 한다.
            await scraper.start()

            total_sites = len(sites)
            completed_count = 0

            # 세마포어로 동시 스크래핑 수 제한 (최대 MAX_CONCURRENT_SCRAPES개)
            _site_durations = {}  # site index -> duration_sec

            async def scrape_with_limit(idx, site):
                _t0 = time.time()
                async with self.scrape_semaphore:
                    outcome_options = {'with_status': True} if self.track_collection_outcome else {}
                    result = await retry_async(scraper.scrape_site, retries=2, delay=2,
                                               site_config=site, known_links=known_links, **outcome_options)
                _site_durations[idx] = round(time.time() - _t0, 1)
                return result

            # Scrape all sites in parallel (with concurrency limit)
            scrape_tasks = [scrape_with_limit(i, site) for i, site in enumerate(sites)]

            logger.info(f"🌐 Scraping {total_sites} sites in parallel (max {self.scrape_semaphore._value} concurrent)...")
            if progress_callback:
                progress_callback(0, total_sites, "병렬 스크래핑 시작...")

            # (UX 2026-07-28) 사이트별 완료 즉시 진행률 갱신 — 이전엔 전체 완료까지
            # 기다린 뒤 일괄 처리해 "0%→90% 급점프"로 보이던 버그를 근본 수정.
            all_raw_items = []
            async for i, result in _cancellable_as_completed(scrape_tasks, stop_event):
                site = sites[i]
                site_name = site['name']
                site_id = site.get('id')
                _site_duration = _site_durations.get(i)
                site_failed = isinstance(result, Exception)
                if self.track_collection_outcome and isinstance(result, dict):
                    site_failed = result.get('state') != 'finished'
                    result = result.get('items', [])
                self.last_run_outcome['sites_processed'] += 1
                self.last_run_outcome['sites_failed'] += int(site_failed)
                if isinstance(result, Exception):
                    logger.error(f"Error scraping {site_name}: {result}")
                    await database.run_db(
                        history_manager.record_scrape_run,
                        site_id=site_id, site_name=site_name,
                        status='error', error_message=str(result)[:500],
                        duration_sec=_site_duration
                    )
                else:
                    count = len(result)
                    logger.info(f"   -> {site_name}: Found {count} items")
                    all_raw_items.extend(result)
                    # 증분 스크래핑: 성공한 사이트의 스크래핑 시각 기록
                    if not site_failed:
                        await database.run_db(history_manager.record_site_scrape, site_name)
                    await database.run_db(
                        history_manager.record_scrape_run,
                        site_id=site_id, site_name=site_name,
                        status='partial' if site_failed else 'success', items_found=count,
                        duration_sec=_site_duration
                    )

                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total_sites, f"{site_name} 스크래핑 완료")

            # Check Stop
            if stop_event and stop_event.is_set():
                self.last_run_outcome['state'] = 'cancelled'
                return []

            # Filter Relevant & New
            relevant_new_items = []
            recheck_candidates = []  # 이미 수집된 항목 — 수정일 재확인 대상
            gate_candidates = []     # 키워드 비매칭 → 의미 게이트로 구제 판정할 후보

            def _route(it):
                """신규/재확인 분기 (히스토리 기준)."""
                if history_manager.is_new(it, history):
                    relevant_new_items.append(it)
                else:
                    recheck_candidates.append(it)

            for item in all_raw_items:
                # 1. Check Exclusions (Negative - Strong Filter)
                if content_filter.is_excluded(item['title']):
                    continue

                # 2. Check Keywords (Positive) & Get Matched
                matched = content_filter.get_matched_keywords(item['title'])
                if matched:
                    item['matched_keyword'] = ",".join(matched)
                    _route(item)
                else:
                    # 키워드 없음 → 의미 게이트 후보 (제목이 프로파일과 유사하면 구제)
                    gate_candidates.append(item)

            # ② 의미 게이트: 키워드 비매칭 항목 중 "도봉구 영유아 복지"와 의미가 가까운 것을 구제
            #    (제목 표현이 달라 키워드에 안 걸린 좋은 정보의 누락 방지 → recall↑)
            # (UX 2026-07-28) 임베딩 API를 항목별로 호출해 수십 초~분 단위가 걸릴 수 있는데
            # 진행률 메시지 없이 조용히 실행되면 "멈춘 것처럼" 보인다(라이브 시연에서 확인).
            if gate_candidates and progress_callback:
                progress_callback(total_sites, total_sites, f"키워드 미매칭 {len(gate_candidates)}개 항목 의미 분석 중...")
            if gate_candidates and ai_client and os.getenv('RELEVANCE_GATE_SEMANTIC', '1') != '0':
                rescued = await self._semantic_gate(gate_candidates, ai_client)
                for item in rescued:
                    item.setdefault('matched_keyword', '의미매칭')
                    _route(item)

            if (not relevant_new_items and not recheck_candidates
                    and not (ai_client and await database.run_db(history_manager.count_ai_backlog))):
                logger.info("No new relevant items found.")
                self.last_run_outcome['state'] = ('partial_failure' if self.last_run_outcome['sites_failed']
                                                   else 'completed')
                return []
                
            # Check Cache: 해당 링크만 DB 조회 (AI 재분석 방지용)
            # load_history() 실패로 history가 비어있으면 get_items_by_links로 이미 수집된 항목을 걸러냄
            uncached_items = []
            candidate_links = [item['link'] for item in relevant_new_items if item.get('link')]
            cached_map = await database.run_db(history_manager.get_items_by_links, candidate_links)

            for item in relevant_new_items:
                cached = cached_map.get(item.get('link'))
                if cached:
                    # 이미 DB에 있는 항목 (load_history 실패로 통과된 경우 등) → 재수집 방지
                    logger.info(f"   -> 이미 수집된 항목 스킵(DB 캐시): {item.get('title', '')[:50]}")
                else:
                    uncached_items.append(item)

            # 최종수정일 재확인: RECHECK_DAYS 이상 지난 기존 항목을 다시 확인
            _RECHECK_DAYS = int(os.getenv('RECHECK_DAYS', '60'))
            _RECHECK_CAP  = int(os.getenv('RECHECK_CAP', '20'))   # 1회 스크래핑당 최대 재확인 수

            if recheck_candidates and _RECHECK_DAYS > 0 and not (stop_event and stop_event.is_set()):
                _recheck_cutoff = datetime.datetime.now() - datetime.timedelta(days=_RECHECK_DAYS)
                recheck_links = [i['link'] for i in recheck_candidates if i.get('link')]
                recheck_cached_map = await database.run_db(history_manager.get_items_by_links, recheck_links)

                _added_recheck = 0
                for item in recheck_candidates:
                    if _added_recheck >= _RECHECK_CAP:
                        break
                    cached = recheck_cached_map.get(item.get('link'))
                    if not cached:
                        continue
                    # Failed/pending analysis is owned by the bounded backlog.
                    # Source-date rechecks must not bypass its cooldown or limit.
                    if (legacy_news_analysis.failure(cached.get('ai_data'))
                            or not legacy_news_analysis.facts(cached.get('ai_data'))):
                        continue
                    stored_at_str = cached.get('scraped_at') or ''
                    try:
                        stored_at = datetime.datetime.fromisoformat(stored_at_str[:19])
                    except (ValueError, TypeError):
                        continue
                    if stored_at < _recheck_cutoff:
                        item['_force_recheck'] = True
                        item['_cached_scraped_at'] = stored_at_str[:10]
                        uncached_items.append(item)
                        _added_recheck += 1
                        days_old = (datetime.datetime.now() - stored_at).days
                        logger.info(f"   -> 재확인 대상({days_old}일 전 수집): {item.get('title', '')[:40]}")

                if _added_recheck > 0:
                    logger.info(f"   -> 최종수정일 재확인 대상 {_added_recheck}개 추가")

            # AI 분석 백로그 우선 재처리 (2026-08-06)
            # 분석 실패 등으로 AI 분석 없이 저장된 항목이, 링크가 이미 DB에 있다는
            # 이유만으로 다음 수집에서 "이미 수집된 항목"으로 걸러져 사이트에 다시 나타나지
            # 않는 한 영구히 재분석되지 않던 문제(실측: 운영 DB 191건, GPT Codex 리뷰로
            # 확인·검증). 신규 항목 분석보다 먼저 최소 물량을 확보해, 신규 유입이 많은 날에도
            # 백로그가 계속 뒤로 밀리지 않게 한다.
            backlog_completed = 0
            backlog_deferred = 0
            backlog_permanent = 0  # (2026-08-06, 3차 GPT Codex 리뷰) "보류(재시도 예정)"와
            analysis_failed = 0
            analysis_deferred = 0
            backlog_links = set()
            # "영구 제외"는 사용자에게 의미가 다르므로 별도로 센다 — 뭉뚱그리면 failed_permanent가
            # 늘어도 "그냥 보류 중"으로 오해하게 된다.
            if ai_client and not (stop_event and stop_event.is_set()):
                try:
                    backlog_reserve = int(utils.get_env_value('AI_BACKLOG_RESERVE') or 30)
                    # Per-run backlog fairness is not a daily API allowance.
                    # Every actual request still uses coordinator admission.
                    backlog_budget = max(0, backlog_reserve)
                    if backlog_budget > 0:
                        backlog_items = await database.run_db(history_manager.get_ai_backlog, backlog_budget)
                        if backlog_items:
                            total_backlog = await database.run_db(history_manager.count_ai_backlog)
                            logger.info(f"📋 [AI 백로그] {len(backlog_items)}건 재분석 시도 (전체 대기 {total_backlog}건)")
                            if progress_callback:
                                progress_callback(total_sites, total_sites,
                                                   f"AI 분석 대기 항목 재처리 중… (0/{len(backlog_items)})")
                            _timeout = aiohttp.ClientTimeout(total=int(os.getenv('AI_REQUEST_TIMEOUT_SEC', '120')))
                            async with aiohttp.ClientSession(timeout=_timeout) as backlog_session:
                                for _bi, b_row in enumerate(backlog_items):
                                    if stop_event and stop_event.is_set():
                                        break
                                    # 백로그 한 건의 처리·저장 실패가 나머지 항목을 막지 않도록
                                    # 항목별로 격리한다. 완료/보류 집계는 DB 저장 성공 후에만 한다.
                                    try:
                                        b_item = dict(b_row)
                                        backlog_links.add(b_item.get('link'))
                                        processed = await self._process_collection_item_ai(
                                            backlog_session, ai_client, b_item, scraper, None)
                                        # 백로그는 신규수집 히스토리와 무관한 기존 레코드 갱신 —
                                        # save_data의 "existing" 갱신 경로를 그대로 재사용
                                        saved = await database.run_db(history_manager.save_data, [processed])
                                        if saved.get('saved') != 1:
                                            raise RuntimeError('AI 백로그 결과를 DB에 저장하지 못했습니다.')
                                        failed = legacy_news_analysis.failure(processed.get('ai_data'))
                                        if failed:
                                            analysis_failed += 1
                                            backlog_deferred += 1
                                        elif legacy_news_analysis.facts(processed.get('ai_data')):
                                            backlog_completed += 1
                                            if (processed.get('is_relevant', True)
                                                    and relevance_filter.passes_post_ai(processed)[0]):
                                                new_items.append(processed)
                                        elif processed.get('ai_skip_reason') == 'failed_permanent':
                                            backlog_permanent += 1
                                        else:
                                            backlog_deferred += 1
                                            analysis_deferred += 1
                                    except Exception as e:
                                        analysis_failed += 1
                                        backlog_deferred += 1
                                        logger.warning(
                                            f"AI 백로그 항목 처리 실패(다음 항목 계속): "
                                            f"{b_row.get('title', '')[:40]} — {e}")
                                    if progress_callback:
                                        progress_callback(total_sites, total_sites,
                                                           f"AI 분석 대기 항목 재처리 중… ({_bi+1}/{len(backlog_items)})")
                            logger.info(
                                f"📋 [AI 백로그] 완료 {backlog_completed}건 · 보류 {backlog_deferred}건 "
                                f"· 영구제외 {backlog_permanent}건")
                except Exception as e:
                    analysis_failed += 1
                    logger.warning(f"AI 백로그 재처리 실패(다음 실행에 재시도): {e}")

            # AI Analysis for newly-scraped items (Throttled & Limited)
            uncached_items = [item for item in uncached_items if item.get('link') not in backlog_links]
            if uncached_items:
                # Check Stop before potentially expensive AI
                if stop_event and stop_event.is_set():
                    self.last_run_outcome['state'] = 'cancelled'
                    return []

                # No app-side daily cap: the provider decides exhaustion.
                # Preserve caches, cancellation and per-request rate admission.
                ai_savings = {'cache': 0, 'semantic': 0, 'quota_skipped': 0, 'analyzed': 0}
                items_to_analyze = uncached_items

                msg = f"AI로 {len(items_to_analyze)}개 항목 분석 중... (Free Tier 안전 모드)"
                logger.info(f"   -> {msg}")
                if progress_callback:
                    progress_callback(total_sites, total_sites, msg)

                # Kept only for the internal call signature. Approximate
                # source vectors cannot authorize copying another notice.
                embed_index = None

                _ai_total_timeout = int(os.getenv('AI_REQUEST_TIMEOUT_SEC', '120'))
                timeout = aiohttp.ClientTimeout(total=_ai_total_timeout)  # 대형 PDF OCR 허용
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    # (버그 2026-07-27) AI 분석 시작 전 progress_callback을 한 번만 호출하고
                    # 그 뒤로는 레이트리미터에 걸려 몇 분씩 걸리는 배치가 끝날 때까지 진행률
                    # 갱신이 전혀 없었다 — policy_sync의 fetch 단계와 동일한 문제(실측: "AI로
                    # N개 항목 분석 중…"에서 화면이 그대로 멈춘 것처럼 보임). 항목이 완료될
                    # 때마다 카운터를 올려 메시지에 반영한다.
                    _ai_done = 0
                    _ai_total_n = len(items_to_analyze)

                    analysis_slots = asyncio.Semaphore(3)

                    async def _process_and_track(_item):
                        nonlocal _ai_done
                        # Bound concurrent page/attachment materialization, not
                        # daily calls. Waiting items do not retain fetched bodies.
                        async with analysis_slots:
                            result = await self._process_collection_item_ai(
                                session, ai_client, _item, scraper, embed_index)
                        _ai_done += 1
                        if progress_callback:
                            progress_callback(
                                total_sites, total_sites,
                                f"AI로 항목 분석 중… ({_ai_done}/{_ai_total_n})")
                        return result

                    ai_tasks = [_process_and_track(item) for item in items_to_analyze]

                    # 취소 가능한 AI 분석 실행
                    processed_items = await _cancellable_gather(
                        ai_tasks, stop_event, "🛑 Scraper stopped during AI analysis."
                    )
                    if processed_items is None:
                        self.last_run_outcome['state'] = 'cancelled'
                        return []

                    # Filter items based on AI relevance check + 절감 집계
                    # 단일 choke point: 캐시/의미중복/정식분류 모든 경로가 여기로 모임
                    for item in processed_items:
                        failed = legacy_news_analysis.failure(item.get('ai_data'))
                        if failed:
                            analysis_failed += 1
                            pending_items.append(item)
                            continue
                        if item.get('ai_cache_hit'):
                            ai_savings['cache'] += 1
                        elif item.get('semantic_dedup'):
                            ai_savings['semantic'] += 1
                        else:
                            ai_savings['analyzed'] += 1
                        if not item.get('is_relevant', True):
                            logger.info(f"      [AI Filter] Dropped {item['title']} (Irrelevant)")
                            continue
                        # 지역(도봉구)+연령(영유아~초등저학년) 정밀 필터
                        _ok, _reason = relevance_filter.passes_post_ai(item)
                        if not _ok:
                            logger.info(f"      [관련성 필터] 제외: {item['title'][:40]} — {_reason}")
                            continue
                        new_items.append(item)

                # Cache reuse avoids duplicate calls; daily denials are not savings.
                _saved = ai_savings['cache'] + ai_savings['semantic']
                if _saved or ai_savings['analyzed']:
                    logger.info(
                        f"💸 [AI 절감] 실제 분석 {ai_savings['analyzed']}건 / 절감 {_saved}건 "
                        f"(캐시 {ai_savings['cache']}, 의미중복 {ai_savings['semantic']})"
                    )
                self.last_ai_savings = {**ai_savings, 'saved_total': _saved}

            # 백로그 재처리 결과는 uncached_items 유무와 무관하게 항상 반영
            self.last_ai_savings = {
                **self.last_ai_savings,
                'backlog_completed': backlog_completed,
                'backlog_deferred': backlog_deferred,
                'backlog_permanent': backlog_permanent,
                'analysis_failed': analysis_failed,
                'analysis_deferred': analysis_deferred,
            }

            # Keep failed/deferred candidates for diagnostics and bounded retries.
            # They must not trigger found-item reports, embeddings or notifications.
            if pending_items:
                await database.run_db(history_manager.save_data, pending_items)
            if new_items:
                # Add update time for all new items
                for item in new_items:
                    if 'scraped_at' not in item:
                        item['scraped_at'] = datetime.datetime.now().isoformat()

                saved_items = await self._save_results(new_items, history)
            else:
                saved_items = []

            incomplete = bool(self.last_run_outcome['sites_failed'] or analysis_failed or analysis_deferred)
            self.last_run_outcome.update(state='partial_failure' if incomplete else 'completed',
                analysis_failed=analysis_failed, analysis_deferred=analysis_deferred, found=len(saved_items))
            _final_msg = f"{'일부 미완료' if incomplete else '완료'} — {len(saved_items)}건 저장/갱신"
            if analysis_failed:
                _final_msg += f" · AI 분석 실패 {analysis_failed}건"
            if analysis_deferred:
                _final_msg += f" · 분석 대기 {analysis_deferred}건"
            if self.last_run_outcome['sites_failed']:
                _final_msg += f" · 수집 실패 {self.last_run_outcome['sites_failed']}개 사이트"
            self.last_run_outcome['message'] = _final_msg
            # DB 저장이 성공한 뒤에만 화면에 최종 완료를 알린다.
            if progress_callback:
                progress_callback(total_sites, total_sites, _final_msg, done=True)
            return saved_items
        except asyncio.CancelledError:
            self.last_run_outcome['state'] = 'cancelled'
            raise
        except Exception:
            self.last_run_outcome['state'] = 'failed'
            raise
                
        finally:
            # 자동 유지보수: 만료 항목 아카이브(숨김) + 보존기간 초과 데이터 정리(1일1회)
            try:
                await database.run_db(history_manager.run_maintenance)
            except Exception as e:
                logger.warning(f"자동 유지보수 실패(무시): {e}")
            _release_scrape_lock(lock_handle)
            await scraper.close()

    async def test_single_site(self, site_config, *, with_status=False):
        """
        Tests scraping for a single site configuration.
        Returns the scraped items without saving them.
        """
        scraper = ScraperEngine(headless=self.headless)
        try:
            await scraper.start()
            logger.info(f"🧪 Testing site: {site_config['name']} ({site_config['url']})")
            # A diagnostic sample is one page, not the user's collection limit.
            # Copy the config so registration and later collection stay unchanged.
            sample = {**site_config, 'max_pages': 1, 'news_article_only': True}
            items = (await scraper.scrape_site(sample, with_status=True) if with_status
                     else await scraper.scrape_site(sample))
            return items
        finally:
            try:
                await asyncio.wait_for(scraper.close(), timeout=10)
            except Exception as error:
                logger.warning("사이트 점검 브라우저 정리 지연 (%s)", type(error).__name__)
            
    async def _semantic_gate(self, candidates, client):
        """키워드 비매칭 항목을 기준 프로파일과 의미 비교해 관련 있는 것만 구제.

        제목 임베딩 vs '도봉구 영유아~아동 복지' 프로파일 임베딩의 코사인 유사도가
        임계값 이상이면 통과시킨다(이후 AI가 본문으로 정밀 재판정). 누락 방지(recall) 목적.
        반환: 구제된 item 리스트.
        """
        from services import embedding_service
        import numpy as _np

        # 임계값 0.83: 실측 캘리브레이션 결과(gemini-embedding-001).
        #   관련 항목(어린이집/도봉구/발달) ~0.85+, 무관(의약품/과학고) ~0.72 → 0.83에서 분리.
        #   경계 항목은 통과시켜도 이후 AI + 지역/연령 필터가 2차로 걸러냄.
        threshold = float(os.getenv('RELEVANCE_SEMANTIC_THRESHOLD', '0.83'))
        cap = int(os.getenv('RELEVANCE_GATE_MAX', '300'))
        cand = candidates[:cap]

        # 설정 화면에서 지역·연령을 저장하면 os.environ이 즉시 갱신된다. 모듈 로드 시점의
        # 상수가 아니라 현재 문장을 매 실행에 만들되, 벡터 자체는 문장별로 재사용한다.
        profile_text = relevance_filter.build_profile_text()
        profile_vec = await embedding_service.embed_semantic_profile_async(profile_text, client=client)
        if profile_vec is None:
            logger.warning("의미 게이트: 프로파일 임베딩 실패 → 게이트 생략(키워드 매칭만 통과)")
            return []
        pv = _np.asarray(profile_vec, dtype='float32')

        async def _score(it):
            await self.embed_rate_limiter.acquire()
            v = await embedding_service.embed_text_async(
                it.get('title', ''), task_type='SEMANTIC_SIMILARITY', client=client
            )
            if v is None:
                return None
            return it, embedding_service.cosine_sim(_np.asarray(v, dtype='float32'), pv)

        results = await asyncio.gather(*[_score(i) for i in cand], return_exceptions=True)
        scored = [r for r in results if r is not None and not isinstance(r, Exception)]
        scored.sort(key=lambda x: x[1], reverse=True)

        # 캘리브레이션용: 점수 분포 + 상·하위 예시 로깅 (임계값을 실데이터로 정하기 위함)
        if scored:
            vals = [s for _, s in scored]
            n = len(vals)
            median = vals[n // 2]
            logger.info(
                f"   -> [게이트 점수분포] 후보 {n}개 | 최고 {vals[0]:.3f} / 중앙 {median:.3f} / 최저 {vals[-1]:.3f} | 임계 {threshold}"
            )
            for it, sc in scored[:8]:
                logger.info(f"        ▲ {sc:.3f}  {str(it.get('title',''))[:50]}")
            if n > 8:
                for it, sc in scored[-3:]:
                    logger.info(f"        ▽ {sc:.3f}  {str(it.get('title',''))[:50]}")

        rescued = []
        for it, score in scored:
            if score >= threshold:
                it['_gate_score'] = round(float(score), 3)
                rescued.append(it)
        logger.info(f"   -> 의미 게이트: 비매칭 {len(cand)}개 중 {len(rescued)}개 구제 (임계 {threshold})")
        return rescued

    async def _process_collection_item_ai(self, session, client, item, scraper=None, embed_index=None):
        """Skip unnecessary fetch/rate waits when provider admission is held.

        This is only an early hint; the classifier still reserves each request.
        Unknown ledger state is held as a usage problem, never a privacy finding.
        """
        from services import ai_coordinator
        reason = None
        try:
            snapshot = ai_coordinator.status()
            models = snapshot.get('models') if isinstance(snapshot, dict) else None
            if not isinstance(models, dict) or snapshot.get('state_warning'):
                raise ValueError('Usage state unavailable')
            selected = models.get(os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite'), {})
            wildcard = models.get('*', {})
            if not isinstance(selected, dict) or not isinstance(wildcard, dict):
                raise ValueError('Invalid model usage state')
            if selected.get('daily_exhausted') or wildcard.get('daily_exhausted'):
                reason = 'daily_quota_exhausted'
        except Exception:
            reason = 'ai_usage_state_unavailable'
        if reason:
            previous = legacy_news_analysis.failure(item.get('ai_data')) or {}
            attempts = min(legacy_news_analysis.MAX_ATTEMPTS, previous.get('attempts', 0) + 1)
            item['ai_last_attempt_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return legacy_news_analysis.mark_failed(item, legacy_news_analysis.failed_result(
                ai_coordinator.AiQuotaStateUnavailable('Usage state held'), reason=reason, attempts=attempts))
        return await self._process_single_item_ai(session, client, item, scraper, embed_index)

    async def _process_single_item_ai(self, session, client, item, scraper=None, embed_index=None):
        """Helper to fetch content and classify a single item.
        scraper: ScraperEngine 인스턴스 (텍스트 부족 시 Vision fallback에 사용)
        embed_index: compatibility-only; approximate matches never supply facts.
        """
        ocr_payload = None
        previous_failure = legacy_news_analysis.failure(item.get('ai_data')) or {}
        attempts = min(legacy_news_analysis.MAX_ATTEMPTS, int(previous_failure.get('attempts', 0)) + 1)
        try:
            # (2026-08-06, GPT Codex 전수조사로 발견) 이 함수는 로그인 페이지 감지·캐시+마감
            # 경과·fetch 자체 실패 등 여러 지점에서 AI 분류 호출 전에 조기 return한다. 예전엔
            # ai_last_attempt_at을 분류 호출 직전(현재 이 위치보다 한참 아래)에서만 찍어서,
            # 이런 조기종료 항목은 "한 번도 시도 안 한 최우선 항목"으로 매 백로그 실행마다
            # 계속 큐 맨 앞을 차지해 뒤 항목을 밀어냈다(재현 테스트로 확인). 함수 진입 직후
            # 한 번만 찍으면 앞으로 생길 조기종료 경로까지 전부 한 번에 막힌다.
            item['ai_last_attempt_at'] = datetime.datetime.now().isoformat()

            # 1. Fetch Content
            content_data = await retry_async(ai_classifier.fetch_page_content_async, retries=2, delay=1, session=session, url=item['link'])
            # PDF/HWPX 텍스트는 무료 API를 쓰기 전에 로컬에서 먼저 읽는다. 실패·스캔·구형
            # HWP 상태도 버리지 않아 빈 HTML 본문만 정상 분석된 것처럼 보이지 않게 한다.
            content_data = await news_attachments.enrich_page_locally(session, content_data, retain_pdf_for_ocr=True)
            # Private in-memory handoff; never copied into item/ai_data or the DB.
            ocr_payload = content_data.pop('_ocr_payload', None)

            text_content = news_attachments.merge_page_text(content_data, include_notices=False)
            images = content_data.get('images', [])
            attachments = content_data.get('attachments', [])
            attachment_items = news_attachments.normalize_attachment_items(
                content_data.get('attachment_items') or attachments)
            attachment_reports = content_data.get('attachment_reports', [])
            attachment_images = [entry['url'] for entry in attachment_items
                                 if entry.get('kind') in news_attachments.IMAGE_KINDS]
            images = list(dict.fromkeys([*images, *attachment_images]))

            # 1-a. 로그인 페이지 리다이렉트 감지 (AI 호출 전 조기 종료)
            # 서버가 인증이 필요한 페이지를 로그인 폼으로 리다이렉트한 경우
            _LOGIN_INDICATORS = [
                '로그인이 필요', '로그인 후 이용', '로그인하세요', '로그인 페이지',
                '아이디와 비밀번호', '아이디/비밀번호', '회원 로그인',
                'Please log in', 'Sign in to continue',
            ]
            if len(text_content) < 2000 and any(ind in text_content for ind in _LOGIN_INDICATORS):
                logger.info(f"   -> 로그인 페이지 리다이렉트 감지, 스킵: {item['title'][:50]}")
                item['is_relevant'] = False
                return item

            # 1-b. 최종수정일 재확인 (force_recheck 플래그가 있는 기존 항목만)
            # 페이지 텍스트에서 '최종수정일' 날짜를 추출해 이전 수집일과 비교
            if item.get('_force_recheck'):
                _page_mod = _extract_page_modified_date(text_content)
                _cached_date_str = item.pop('_cached_scraped_at', '') or ''
                item.pop('_force_recheck', None)
                try:
                    _cached_date = datetime.date.fromisoformat(_cached_date_str[:10])
                except (ValueError, TypeError):
                    _cached_date = None

                if not _page_mod:
                    logger.info(f"   -> 재확인: 최종수정일 없음, 스킵: {item.get('title', '')[:40]}")
                    item['is_relevant'] = False
                    return item

                if _cached_date and _page_mod <= _cached_date:
                    logger.info(f"   -> 재확인: 수정일({_page_mod}) <= 수집일({_cached_date}), 변경없음: {item.get('title', '')[:40]}")
                    item['is_relevant'] = False
                    return item

                logger.info(f"   -> 재확인: 수정일({_page_mod}) > 수집일({_cached_date}), 업데이트 재분석: {item.get('title', '')[:40]}")
                item['page_modified_date'] = _page_mod.isoformat()

            # 2. Attachment / OCR Analysis
            # Strategy:
            # 1. If text is very short (< 300 chars), prioritize Images (Poster)
            # 2. Check Attachments (PDFs) - often contain the real details

            has_enough_text = len(text_content) > 500
            has_pdfs = any(report.get('kind') == 'pdf' for report in attachment_reports)
            has_images = bool(images)

            # 1-a2. 저비용 렌더 텍스트 fallback (Gemini 불필요):
            #   정적 fetch가 빈 껍데기만 받는 JS/iframe 상세페이지(NHIS '?mode=view' 등)를
            #   Playwright로 렌더해 본문 텍스트를 추출. Vision OCR(비쌈) 이전에 먼저 시도.
            if not has_enough_text and scraper and item.get('link'):
                rendered = await scraper.fetch_rendered_text(item['link'])
                original_page_text = str(content_data.get('text') or '')
                if rendered and len(rendered) > max(len(original_page_text), 300):
                    logger.info(f"   -> 렌더 텍스트 추출 {len(rendered)}자 (정적 {len(original_page_text)}자→보강): {item['title'][:30]}")
                    content_data['text'] = rendered
                    text_content = news_attachments.merge_page_text(content_data, include_notices=False)
                    has_enough_text = len(text_content) > 500

            # 1-b. Vision 하이브리드 (JS 렌더링 전용):
            #   - 포스터 이미지가 있으면 → 아래 섹션 B의 고화질 이미지 OCR이 더 정확
            #   - PDF가 있으면          → 섹션 A의 PDF OCR이 처리
            #   - 둘 다 없고 텍스트 부족 → JS 렌더링 페이지 → Playwright Vision 투입
            if (not has_enough_text and scraper and item.get('link')
                    and not has_images and not has_pdfs and not attachment_items):
                logger.info(f"   -> JS 렌더링 의심({len(text_content)}자, 이미지/PDF 없음), Vision 투입: {item['title'][:30]}...")
                vision_md = await scraper.capture_page_vision(item['link'])
                if vision_md:
                    text_content = f"--- [VISION ANALYSIS] ---\n{vision_md}\n--- [END VISION] ---\n\n{text_content}"
                    has_enough_text = True

            # --- Intelligent AI Caching Check ---
            # Hash the pure source text to see if we've analyzed exactly this before
            # 본문이 그대로여도 기관이 같은 주소의 첨부파일만 교체할 수 있다. 실제 파일
            # 지문을 캐시 키에 포함해 예전 분석이 새 첨부에 재사용되는 문제를 막는다.
            attachment_fingerprint = str(content_data.get('attachment_fingerprint') or '')
            attachment_identity = json.dumps(attachment_items, ensure_ascii=False, sort_keys=True) if attachment_items else ''
            # Coverage changes must invalidate old "complete" AI results even if the
            # retained prefix and file bytes are identical. Do not hash transient cache hits.
            attachment_assessment = json.dumps([
                {key: report.get(key) for key in ('url', 'kind', 'status', 'coverage')}
                for report in attachment_reports
            ], ensure_ascii=False, sort_keys=True) if attachment_reports else ''
            from services import news_ai_input, page_coverage
            input_limit = news_ai_input.content_limit()
            source_page = page_coverage.normalize(content_data.get('source_page'))
            # Older fallback hashes may hold facts copied from another notice by
            # approximate embedding similarity. Revalidate only when encountered
            # again; do not delete data or invalidate the current profile workflow.
            from services import ai_coordinator
            request_budget = ai_coordinator.input_token_budget(os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite'))
            input_recipe = (f'{news_ai_input.RECIPE}:{input_limit}:{page_coverage.RECIPE}:'
                            f'{news_attachments.OCR_SELECTION_RECIPE}:exact-source-cache-v3-request-budget:{request_budget}')
            hash_input = (item['title'] + text_content + attachment_fingerprint + attachment_identity
                          + attachment_assessment + input_recipe
                          + json.dumps(source_page, sort_keys=True, ensure_ascii=False)).encode('utf-8')
            content_hash = hashlib.md5(hash_input).hexdigest()
            item['content_hash'] = content_hash

            cached_ai = await database.run_db(history_manager.find_cached_ai_data_by_hash, content_hash)
            # Error/budget fallbacks are pending work, not completed analysis.
            # Keep their stored bytes, but allow a later budgeted attempt. A
            # normal category with is_relevant=False remains a valid cache hit.
            cached_category = cached_ai.get('category') if isinstance(cached_ai, dict) else None
            if (isinstance(cached_category, str) and cached_category.strip()
                    and cached_category.strip() != '미분류'
                    and not legacy_news_analysis.failure(cached_ai)):
                logger.info(f"♻️ [AI Cache Hit] Bypassing Gemini request for exact duplicate: {item['title']}")
                item['ai_cache_hit'] = True  # 절감 카운트용 플래그
                item.update(cached_ai)
                # (2026-08-06, GPT Codex 지적) ai_data 저장을 마감 판정보다 먼저 해야 한다 —
                # AI 분류 결과 자체는 유효한데, 예전엔 마감 경과 시 저장 없이 조기 return해서
                # 백로그 재처리 중이면 이 항목이 "AI 결과가 이미 있어도" 영원히
                # ai_data IS NULL 상태로 남아 매 실행마다 다시 뽑혀 재처리되는 낭비가 있었다.
                item['ai_data'] = json.dumps(cached_ai, ensure_ascii=False)
                item['ai_skip_reason'] = None
                # Serializing list fields to comma strings (same as post-classification)
                for col in ['target_characteristics', 'life_cycle', 'residence', 'related_urls']:
                    if isinstance(item.get(col), list):
                        item[col] = ','.join(item[col])
                # 캐시된 분류에 마감/행사종료일이 들어 있으면 경과 여부 확인 (동일 내용이므로 신뢰 가능)
                if _is_past_deadline(item):
                    logger.info(f"   -> [캐시] 마감 경과로 수집 제외: {item.get('title','')[:40]}")
                    item['is_relevant'] = False
                    return item
                return item
            # --- End Cache Check ---

            # Similarity is a search aid, never proof that notices share facts.
            # Only the exact cache above skips classification. Search vectors
            # are still prepared after saving by _save_results, not here.

            # A. 로컬 텍스트가 거의 없는 스캔 PDF만 Gemini OCR로 보낸다.
            # Limit to 1 PDF to save time/cost
            pdf_attachments = [report for report in attachment_reports
                               if report.get('kind') == 'pdf' and report.get('status') == 'ocr_required']
            if pdf_attachments:
                from services import ocr_service
                target_pdf = pdf_attachments[0]['url']
                logger.info(f"📄 Analyzing PDF Attachment: {target_pdf}...")
                await self.ocr_rate_limiter.acquire()
                try:
                    if ocr_payload is not None and ocr_payload.url == target_pdf and ocr_payload.data is not None:
                        ocr_result = await ocr_service.analyze_downloaded_document(ocr_payload.data)
                    else:
                        ocr_result = await ocr_service.analyze_image_or_pdf(target_pdf, mime_type='application/pdf')
                finally:
                    if ocr_payload is not None:
                        ocr_payload.close()

                if ocr_result.get('is_relevant'):
                    pdf_attachments[0]['status'] = 'ocr_complete'
                    item['ocr_analyzed'] = True
                    # Prepend PDF content as it's likely more important than generic web footer
                    text_content = f"--- [PDF CONTENT START] ---\n{json.dumps(ocr_result, ensure_ascii=False)}\n--- [PDF CONTENT END] ---\n\n" + text_content
                else:
                    pdf_attachments[0]['status'] = (
                        'ai_transmission_held' if ocr_result.get('error_code') == ocr_service.DOCUMENT_REVIEW_CODE
                        else 'ocr_failed')

            # Explicit pending image attachments matter even when the HTML is long.
            # PDF and image share a maximum of two OCR attempts per post.
            ocr_images = news_attachments.select_ocr_images(
                attachment_reports, images, has_enough_text=has_enough_text,
                pdf_selected=bool(pdf_attachments))
            if ocr_images:
                from services import ocr_service
                # Try up to 2 images
                for img_url in ocr_images:
                    logger.info(f"🖼️ Analyzing Poster Image: {img_url}...")
                    await self.ocr_rate_limiter.acquire()
                    ocr_result = await ocr_service.analyze_image_or_pdf(img_url)
                    if ocr_result.get('is_relevant'):
                        for report in attachment_reports:
                            if report.get('url') == img_url:
                                report['status'] = 'ocr_complete'
                        item['ocr_analyzed'] = True
                        text_content = f"--- [POSTER IMAGE CONTENT] ---\n{json.dumps(ocr_result, ensure_ascii=False)}\n--- [POSTER END] ---\n\n" + text_content
                    else:
                        for report in attachment_reports:
                            if report.get('url') == img_url:
                                report['status'] = (
                                    'ai_transmission_held' if ocr_result.get('error_code') == ocr_service.DOCUMENT_REVIEW_CODE
                                    else 'ocr_failed')

            # 3. Classify (429 재시도는 classify_post_async 내부에서 처리)
            # Rate Limiter 획득: 캐시 히트/OCR 처리 후 실제 Gemini 호출 직전에만 소비
            await self.ai_rate_limiter.acquire()
            classification = await ai_classifier.classify_post_async(
                                               client=client, title=item['title'], link=item['link'],
                                               site_name=item.get('site_name', ''), content=text_content,
                                               content_max_chars=input_limit, source_page=source_page,
                                               strict=True, request_attempts=1)
            # Supports an older/non-strict adapter without treating its fallback
            # as a successful analysis. Ordinary is_relevant=False is still valid.
            if legacy_news_analysis.failure(classification):
                return legacy_news_analysis.mark_failed(item,
                    legacy_news_analysis.failed_result(reason='analysis_error', attempts=attempts))
            # 기존 ai_data 안에 보존하므로 DB 스키마를 바꾸거나 사용자 저장 상태를 덮지 않는다.
            # 모델이 임의로 만든 값이 아니라 실제 첨부 처리 결과로 항상 덮어쓴다.
            if attachment_reports:
                classification['source_attachments'] = news_attachments.summarize_attachment_reports(attachment_reports)
            item['ai_call_at'] = datetime.datetime.now().isoformat()
            item['ai_skip_reason'] = None  # 성공했으니 이전(재확인 전)의 백로그 사유가 있었다면 해제

            item.update(classification)

            # 로그인 페이지 제목으로 덮어씌워진 경우 사후 차단
            # AI가 리다이렉트된 로그인 페이지 내용을 기반으로 제목을 바꿨을 때 처리
            _updated_title = item.get('title', '')
            if '로그인' in _updated_title and ('회원' in _updated_title or '비밀번호' in _updated_title or '<' in _updated_title):
                logger.info(f"   -> AI 분류 후 로그인 제목 감지, 스킵: {_updated_title[:60]}")
                item['is_relevant'] = False
                # (2026-08-06, GPT Codex 2차 리뷰) failed_permanent는 AI 오분류·일시적 리다이렉트
                # 한 번만으로 실제 복지정보를 영구히 자동분석 대상에서 뺄 위험이 있다(초기화·재시도
                # 수단이 아직 없음). AI가 만든 제목만 보고 판정하지 말고, 원문(text_content)
                # 자체에도 동일한 로그인 신호가 있을 때만 "구조적으로 막힌 사이트"로 신뢰해 영구
                # 제외한다 — 원문엔 신호가 없는데 AI만 로그인으로 오판했다면 재시도 가능 상태로
                # 남겨(ai_skip_reason=None) 다음 실행에서 공정하게 다시 시도되게 한다.
                if any(ind in text_content for ind in _LOGIN_INDICATORS):
                    item['ai_skip_reason'] = 'failed_permanent'
                return item

            # Serialize full AI result for Notion-style reports
            # (2026-08-06, GPT Codex 지적) 마감 판정보다 먼저 저장해야 한다 — AI 분류 결과
            # 자체는 유효한데, 예전엔 마감 경과 시 저장 없이 조기 return해서 백로그 재처리
            # 중이면 이 항목이 "AI 결과가 이미 있어도" 영원히 ai_data IS NULL로 남아
            # 매 실행마다 다시 뽑혀 재처리되는 낭비가 있었다.
            item['ai_data'] = json.dumps(classification, ensure_ascii=False)

            # Flatten list fields to comma-separated strings for DB columns
            if isinstance(item.get('target_characteristics'), list):
                item['target_characteristics'] = ','.join(item['target_characteristics'])
            if isinstance(item.get('life_cycle'), list):
                item['life_cycle'] = ','.join(item['life_cycle'])
            if isinstance(item.get('residence'), list):
                item['residence'] = ','.join(item['residence'])
            if isinstance(item.get('related_urls'), list):
                item['related_urls'] = ','.join(item['related_urls'])

            # 마감일/행사종료일 경과 필터 (공유 헬퍼 _is_past_deadline 사용)
            # - deadline: 프로그램 신청 마감일 → 경과 시 스킵
            # - event_end_date: 행사 종료일 → 경과 시 스킵 (event_date는 시작일이므로 제외)
            # - 둘 다 없거나 파싱 불가하면 상시 모집으로 간주하여 수집
            if _is_past_deadline(item):
                logger.info(f"   -> 마감 경과, 스킵: {item.get('title','')[:40]} (deadline={item.get('deadline')}, event_end_date={item.get('event_end_date')})")
                item['is_relevant'] = False
                return item

            # Related Site Discovery Logic
            source_name = classification.get('source_site_name')
            source_url = classification.get('source_site_url')
            if source_url and source_url.startswith('http'):
                logger.info(f"🔗 [Discovery] Found potential new site: {source_name} ({source_url})")
                # Future: Add to discovered_sites table

            logger.info(f"      [AI] {item['title']} -> {item.get('category', '?')}")
            return item
        except Exception as e:
            logger.error(f"Error in AI process for {item.get('title')}: {e}")
            return legacy_news_analysis.mark_failed(item,
                legacy_news_analysis.failed_result(e, attempts=attempts) if client else
                legacy_news_analysis.failed_result(reason='authentication_required', attempts=0))
        finally:
            # Includes cache hits, early filters, exceptions and task cancellation.
            if ocr_payload is not None:
                ocr_payload.close()

    async def _save_results(self, new_items, history):
        """DB에 실제 저장된 항목만 리포트와 알림에 전달하고 그 목록을 반환한다."""
        logger.info(f"✨ Found {len(new_items)} new items")

        # Save Data (DB handles Insert/Update)
        try:
            save_result = await database.run_db(history_manager.save_data, new_items)
        except Exception as e:
            logger.error(f"Failed to save data to DB: {e}")
            raise

        saved_links = set(save_result.get('saved_links') or [])
        saved_items = [item for item in new_items if item.get('link') in saved_links]
        self.last_save_result = {
            key: save_result.get(key, 0)
            for key in ('requested', 'saved', 'processed', 'skipped')
        }
        if not saved_items:
            logger.info("DB에 새로 저장되거나 갱신된 항목이 없어 후속 알림을 건너뜁니다.")
            return []

        # 분류가 끝난 구조화 필드를 사용해 검색용 벡터를 같은 기준으로 다시 만든다.
        # 앞 단계의 제목+원문 벡터는 중복 제거 전용이며 검색 순위에는 섞지 않는다.
        try:
            from services import embedding_service
            if not embedding_service.get_unavailable_reason():
                for start in range(0, len(saved_items), 20):
                    chunk = saved_items[start:start + 20]
                    texts = [embedding_service.build_search_embedding_text(item) for item in chunk]
                    await self.embed_rate_limiter.acquire()
                    vectors = await embedding_service.embed_texts_batch_async(
                        texts, task_type='RETRIEVAL_DOCUMENT')
                    for item, vector in zip(chunk, vectors):
                        if vector is None or not item.get('link'):
                            continue
                        await database.run_db(
                            history_manager.save_embedding,
                            item['link'], embedding_service.to_blob(vector),
                            embedding_service.SEARCH_EMBEDDING_RECIPE,
                        )
        except Exception as e:
            # 수집 결과 자체는 이미 안전하게 저장됐다. 검색 벡터만 다음 백필 대상으로 남긴다.
            logger.warning(f"검색용 임베딩 후처리 실패(수집 결과는 유지): {e}")

        # Generate Report (파일 I/O → 스레드풀에서 실행)
        try:
            report_path = await database.run_db(notifier.generate_report, saved_items)
            logger.info(f"Report generated: {report_path}")
        except Exception as e:
            logger.error(f"Report generation failed: {e}")

        # Notify (공유 세션으로 Discord 전송)
        try:
            await database.run_db(notifier.send_notification, len(saved_items))
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as notify_session:
                await notifier.send_discord_notification(saved_items, session=notify_session)
                # Google Chat 채널 (GOOGLE_CHAT_WEBHOOK_URL 설정 시)
                await notifier.send_google_chat_notification(saved_items, session=notify_session)
                # 마감 임박 항목 별도 알림 (스크래핑 완료 시 1회, 설정된 모든 채널)
                await notifier.notify_expiring_items(session=notify_session)
        except Exception as e:
            logger.error(f"Notification error: {e}")
        return saved_items

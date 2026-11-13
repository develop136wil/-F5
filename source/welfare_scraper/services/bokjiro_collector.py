"""복지로 영유아 복지서비스 일괄 수집기 (API 기반, AI 불필요).

복지로 서비스 목록(moveTWAT52005M)은 JS SPA지만 내부 JSON API
(selectWlfareInfo.do)를 직접 호출해 페이지네이션 전체를 받아온다.
서버측 생애주기 필터가 세션 기반이라 직접 적용이 안 되므로, 전체를 받아
응답의 RETURN_STR(BKJR_LFTM_CYC_CD 태그)에 '영유아'가 포함된 항목만 추린다.

→ "영유아 전부 파악"을 저비용으로 달성(AI 분류 없이 API가 제목/요약/태그 제공).
detail URL은 moveTWAT52011M.do?wlfareInfoId=<ID> (복지서비스 상세).
"""
import json
import datetime
import hashlib
import asyncio
from services.public_transport import new_public_context
from typing import List, Dict, Any, Optional

from services.logger import setup_logger

logger = setup_logger(__name__)

LIST_PAGE = "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52005M.do"
API = "https://www.bokjiro.go.kr/ssis-tbu/TWAT52005M/twataa/wlfareInfo/selectWlfareInfo.do"
DETAIL = "https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52011M.do?wlfareInfoId="

PAGE_SIZE = 9

# INTRS_THEMA_CD(관심주제) → 앱 카테고리 매핑
_THEME_MAP = [
    ("신체건강", "의료/재활"), ("정신건강", "의료/재활"),
    ("교육", "교육/보육"), ("보육", "교육/보육"),
    ("보호·돌봄", "돌봄/양육"), ("돌봄", "돌봄/양육"),
    ("생활지원", "생활지원"), ("일자리", "생활지원"),
]


def _parse_return_str(s: str) -> Dict[str, str]:
    """RETURN_STR("KEY:v1,v2;KEY2:...")를 dict로 파싱."""
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _map_category(themes: str) -> str:
    for kw, cat in _THEME_MAP:
        if kw in (themes or ""):
            return cat
    return "복지"


# 영유아-중심 판정: 영유아 외 생애주기 허용 집합 (영유아 복지와 자연 겹침)
_NEAR_INFANT = {"영유아", "아동", "임신·출산", "임신.출산", "임신출산"}
# 성인/노인 전용 — 제목에 있으면 영유아 무관으로 간주(제외)
_ADULT_TITLE_KW = (
    "대학생", "등록금", "학자금", "장학", "귀농", "창업", "취업", "실업",
    "소상공인", "근로자", "보훈", "유공", "참전", "경로당", "노인일자리",
    "어르신", "중장년", "고령", "은퇴", "농어업인",
)


import re as _re_bk
from urllib.parse import quote as _url_quote

# 광역시/도 약칭
_SIDO_SHORT = [
    ("서울특별시", "서울"), ("부산광역시", "부산"), ("대구광역시", "대구"), ("인천광역시", "인천"),
    ("광주광역시", "광주"), ("대전광역시", "대전"), ("울산광역시", "울산"), ("세종특별자치시", "세종"),
    ("경기도", "경기"), ("강원특별자치도", "강원"), ("강원도", "강원"), ("충청북도", "충북"),
    ("충청남도", "충남"), ("전북특별자치도", "전북"), ("전라북도", "전북"), ("전라남도", "전남"),
    ("경상북도", "경북"), ("경상남도", "경남"), ("제주특별자치도", "제주"),
]
_GU_RE = _re_bk.compile(r'([가-힣]+[시군구])')
_SIDO_FULLS = {full for full, _ in _SIDO_SHORT}

# 도봉 주민이 실제 활용 가능한 지역만 보존 (전국/서울+자치구/도봉/인접 강북·노원).
# 그 외 타지역 지자체 정책은 노이즈 → 수집 단계에서 차단.
_KEEP_REGION_RE = _re_bk.compile(r'(전국|서울|도봉|강북|노원)')
_SERVICE_ID_RE = _re_bk.compile(r'[A-Za-z0-9_-]{1,120}')


def _extract_region(biz: str, addr: str):
    """담당기관/주소에서 지역 라벨 추출. (label, residence) 반환.

    지자체 서비스면 ('도봉구' 등, '서울 도봉구'), 중앙/재단이면 ('', '전국').
    광역시/특별시 자체(예: '서울특별시')는 시군구로 오인하지 않는다.
    """
    text = f"{biz or ''} {addr or ''}".strip()
    sido = next((short for full, short in _SIDO_SHORT if full in text), "")
    gu = ""
    for g in _GU_RE.findall(text):
        if g in ('재단법인', '사단법인', '보건소'):
            continue
        if g in _SIDO_FULLS or g.endswith(('광역시', '특별시', '특별자치시')):
            continue                 # 시도 자체는 '구'가 아님 ('서울 서울특별시' 중복 방지)
        if g.endswith(('시', '군', '구')):
            gu = g
            break
    if not sido and not gu:
        return "", "전국"            # 중앙부처/민간재단 → 전국
    label = gu or sido
    residence = f"{sido} {gu}".strip() if (sido and gu) else (sido or gu)
    return label, residence


def _is_target_region(residence: str) -> bool:
    """도봉 활용권(전국/서울/도봉/강북/노원) 또는 미상만 통과. 타지역은 제외."""
    if not residence or not residence.strip():
        return True                  # 지역 미상 → 보수적으로 통과
    return bool(_KEEP_REGION_RE.search(residence))


def _is_infant_focused(cycles_str: str, title: str) -> bool:
    """영유아-중심 항목만 통과. 다생애주기 광범위 서비스/성인전용 제목 배제."""
    cycles = {c.strip() for c in (cycles_str or "").split(",") if c.strip()}
    if "영유아" not in cycles:
        return False
    # 영유아·아동·임신출산 외 생애주기가 3개 이상 → 영유아는 부수적 → 제외
    others = cycles - _NEAR_INFANT
    if len(others) >= 3:
        return False
    # 성인/노인 전용 키워드가 제목에 있으면 제외
    if any(k in (title or "") for k in _ADULT_TITLE_KW):
        return False
    return True


def _split_tags(value: str) -> set[str]:
    return {part.strip() for part in (value or '').split(',') if part.strip()}


def _official_tags(return_str: str) -> Dict[str, list[str]]:
    """Extract only known public Bokjiro taxonomy labels from one result row."""
    from services.news_profile_taxonomy import (
        LIFE_CYCLES, HOUSEHOLD_SITUATIONS, INTEREST_TOPICS,
        canonical_life_cycle, canonical_household_situation,
    )
    parsed = _parse_return_str(return_str)
    life = {canonical_life_cycle(value) for value in
            _split_tags(parsed.get('BKJR_LFTM_CYC_CD', ''))}
    # The household field name has changed across Bokjiro revisions. Scan tag
    # values but only retain labels from the public allowlist.
    all_values = {tag for value in parsed.values() for tag in _split_tags(value)}
    households = {canonical_household_situation(value) for value in all_values}
    topics = _split_tags(parsed.get('INTRS_THEMA_CD', ''))
    return {
        'life_cycles': sorted(life & set(LIFE_CYCLES)),
        'household_situations': sorted(households & set(HOUSEHOLD_SITUATIONS)),
        'interest_topics': sorted(topics & set(INTEREST_TOPICS)),
    }


def normalize_official_service(row: dict) -> Dict[str, Any] | None:
    """Normalize one provider row without inventing eligibility facts.

    Provider/address location is useful evidence but does not prove the service's
    residence requirement. Age bounds are likewise absent from the list API, so
    both remain explicitly unknown for the later review boundary.
    """
    if not isinstance(row, dict):
        return None
    service_id = str(row.get('WLFARE_INFO_ID') or '').strip()
    title = str(row.get('WLFARE_INFO_NM') or '').strip()
    if not _SERVICE_ID_RE.fullmatch(service_id) or not title:
        return None
    provider = str(row.get('BIZ_CHR_INST_NM') or '').strip()
    address = str(row.get('ADDR') or '').strip()
    region_label, provider_region = _extract_region(provider, address)
    # _extract_region returns 전국 when no regional evidence exists for the
    # historical infant rule. Do not reuse that assumption as eligibility here.
    if not region_label:
        provider_region = ''
    return {
        'source_type': 'bokjiro_service',
        'source_id': service_id,
        'source_key': f'bokjiro:{service_id}',
        'title': title[:300],
        'summary': str(row.get('WLFARE_INFO_OUTL_CN') or '').strip()[:2000],
        'provider': provider[:200],
        'provider_region': provider_region[:100],
        'eligibility_region': '',
        'age_range_known': False,
        'link': DETAIL + _url_quote(service_id, safe=''),
        **_official_tags(str(row.get('RETURN_STR') or '')),
    }


def _matches_official_tags(return_str: str, *, life_cycles=(), characteristics=(),
                           interest_topics=()) -> bool:
    from services.news_profile_taxonomy import (
        LIFE_CYCLES, HOUSEHOLD_SITUATIONS, INTEREST_TOPICS,
        canonical_life_cycle, canonical_household_situation,
    )
    requested_life = {canonical_life_cycle(value) for value in life_cycles}
    requested_households = {canonical_household_situation(value) for value in characteristics}
    requested_topics = {value.strip() for value in interest_topics if isinstance(value, str) and value.strip()}
    if (requested_life - set(LIFE_CYCLES) or requested_households - set(HOUSEHOLD_SITUATIONS)
            or requested_topics - set(INTEREST_TOPICS)):
        raise ValueError('복지로에서 제공하는 생애주기·가구상황·관심주제를 선택해주세요.')
    if not requested_life and not requested_households and not requested_topics:
        raise ValueError('복지로 생애주기·가구상황·관심주제 중 하나 이상 선택해주세요.')
    tags = _official_tags(return_str)
    return ((not requested_life or requested_life & set(tags['life_cycles']))
            and (not requested_households or
                 requested_households & set(tags['household_situations']))
            and (not requested_topics or requested_topics & set(tags['interest_topics'])))


def _build_body(page: int, *, life_cycles=(), characteristics=(), interest_topics=()) -> str:
    return json.dumps({
        "dmSearchParam": {
            "page": str(page), "onlineYn": "", "searchTerm": "", "tabId": "1",
            "orderBy": "date", "bkjrLftmCycCd": "",
            "daesang": ",".join(characteristics), "period": ",".join(life_cycles),
            "age": "", "region": "", "jjim": "", "subject": ",".join(interest_topics), "favoriteKeyword": "",
            "sidoCd": "", "sggCd": "", "endYn": "N",
        },
        "dmScr": {"curScrId": "tbu/app/twat/twata/twataa/TWAT52005M", "befScrId": ""},
    })


def _official_page(payload, page_number, *, life_cycles=(), characteristics=(), interest_topics=()):
    """Validate a whole production page; missing/malformed data is not an empty end."""
    if not isinstance(payload, dict) or not isinstance(payload.get('dmCount'), dict):
        raise ValueError('Invalid Bokjiro page metadata')
    raw_total = payload['dmCount'].get('dsServiceList0Count')
    if isinstance(raw_total, bool) or not str(raw_total).isdigit():
        raise ValueError('Invalid Bokjiro total')
    total = int(raw_total)
    rows = payload.get('dsServiceList0')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Invalid Bokjiro rows')
    items, source_ids = [], []
    for row in rows:
        item = normalize_official_service(row) if isinstance(row, dict) else None
        if item is None:
            raise ValueError('Invalid Bokjiro service row')
        source_ids.append(item['source_id'])
        if not _matches_official_tags(row.get('RETURN_STR', ''), life_cycles=life_cycles,
                characteristics=characteristics, interest_topics=interest_topics):
            continue
        items.append({'service_id': item['source_id'], 'title': item['title'],
            'summary': item['summary'], 'provider': item['provider'], 'link': item['link'],
            'life_cycles': item['life_cycles'], 'household_situations': item['household_situations'],
            'interest_topics': item['interest_topics']})
    return {'state': 'page', 'page': page_number, 'items': items, 'scanned': len(rows),
            'total_reported': total, 'complete': not rows,
            'fingerprint': hashlib.sha256(json.dumps(sorted(source_ids)).encode()).hexdigest() if rows else ''}


async def iter_official_pages(*, life_cycles=(), characteristics=(), interest_topics=(),
                              start_page=1, max_pages=5):
    """Read a bounded page window, yielding each page before requesting the next.

    Unlike a preview this has no result-count cap. The caller persists a cursor.
    An empty, valid final page is required; provider errors never become completion.
    """
    if type(start_page) is not int or start_page < 1 or type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise ValueError('Invalid Bokjiro page window')
    _matches_official_tags('BKJR_LFTM_CYC_CD:', life_cycles=life_cycles,
                          characteristics=characteristics, interest_topics=interest_topics)
    from scraper_engine import ScraperEngine
    from services.scraper_service import _acquire_scrape_lock, _release_scrape_lock
    from services import app_lifecycle
    from services.news_analysis_worker import _settle_db
    handle, cancelled = await _settle_db(asyncio.to_thread(_acquire_scrape_lock))
    if cancelled:
        if handle is not None:
            _release_scrape_lock(handle)
        raise asyncio.CancelledError
    if handle is None:
        yield {'state': 'busy'}
        return
    eng = None
    try:
        eng = ScraperEngine(headless=True)
        await eng.start()
        ctx = await new_public_context(eng.browser)
        page = await ctx.new_page()
        await page.goto(LIST_PAGE, wait_until='domcontentloaded', timeout=40000)
        for page_number in range(start_page, start_page + max_pages):
            if app_lifecycle.shutdown_requested:
                yield {'state': 'shutdown'}
                return
            response = await page.request.post(API,
                data=_build_body(page_number, life_cycles=life_cycles,
                    characteristics=characteristics, interest_topics=interest_topics),
                headers={'Content-Type': 'application/json'}, timeout=30000)
            if not response.ok:
                raise ValueError('Bokjiro page request failed')
            result = _official_page(await response.json(), page_number, life_cycles=life_cycles,
                characteristics=characteristics, interest_topics=interest_topics)
            yield result
            if result['complete']:
                return
            # Sequential requests, not an unbounded concurrent crawler.
            await asyncio.sleep(0.25)
    finally:
        try:
            if eng is not None:
                await eng.close()
        finally:
            _release_scrape_lock(handle)


async def collect_official_sample(*, life_cycles=(), characteristics=(), interest_topics=(),
                                  max_pages: int = 3, limit: int = 20) -> Dict[str, Any]:
    """Read a small, non-persistent sample for calibration; no AI or DB writes."""
    if type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise ValueError('표본 조회 페이지는 1~5 사이여야 합니다.')
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('표본 개수는 1~30 사이여야 합니다.')
    # Validate before starting a browser or taking the process scrape lock.
    _matches_official_tags('BKJR_LFTM_CYC_CD:', life_cycles=life_cycles,
                           characteristics=characteristics, interest_topics=interest_topics)

    from scraper_engine import ScraperEngine
    from services.scraper_service import _acquire_scrape_lock, _release_scrape_lock
    handle = await asyncio.to_thread(_acquire_scrape_lock)
    if handle is None:
        return {'state': 'busy', 'items': [], 'saved': False, 'ai_called': False}
    eng = None
    items = []
    seen = set()
    scanned = failed_pages = total_reported = attempted_pages = 0
    try:
        eng = ScraperEngine(headless=True)
        await eng.start()
        ctx = await new_public_context(eng.browser)
        page = await ctx.new_page()
        # 이 화면은 로드 뒤에도 백그라운드 통신이 이어질 수 있어 networkidle이 오지 않는
        # 경우가 있다. 목록 DOM과 세션만 준비되면 아래 JSON API를 바로 호출할 수 있다.
        await page.goto(LIST_PAGE, wait_until='domcontentloaded', timeout=40000)
        for page_number in range(1, max_pages + 1):
            attempted_pages += 1
            try:
                response = await page.request.post(API,
                    data=_build_body(page_number, life_cycles=life_cycles,
                                     characteristics=characteristics,
                                     interest_topics=interest_topics),
                    headers={'Content-Type': 'application/json'})
                payload = await response.json()
                if page_number == 1:
                    total_reported = int((payload.get('dmCount') or {}).get('dsServiceList0Count', 0) or 0)
                rows = payload.get('dsServiceList0', [])
                if not isinstance(rows, list):
                    raise ValueError('invalid rows')
                for row in rows:
                    scanned += 1
                    if not isinstance(row, dict) or not _matches_official_tags(
                            row.get('RETURN_STR', ''), life_cycles=life_cycles,
                            characteristics=characteristics,
                            interest_topics=interest_topics):
                        continue
                    item = normalize_official_service(row)
                    if item is None or item['source_key'] in seen:
                        continue
                    seen.add(item['source_key'])
                    # Keep the existing public sample response compact while all
                    # fields originate from the same normalized trust boundary.
                    items.append({
                        'service_id': item['source_id'], 'title': item['title'],
                        'summary': item['summary'][:500], 'provider': item['provider'],
                        'link': item['link'], 'life_cycles': item['life_cycles'],
                        'household_situations': item['household_situations'],
                        'interest_topics': item['interest_topics'],
                    })
                    if len(items) >= limit:
                        break
                if len(items) >= limit or not rows:
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                failed_pages += 1
        if attempted_pages and failed_pages == attempted_pages and scanned == 0:
            raise RuntimeError('all Bokjiro sample pages failed')
        return {'state': 'finished', 'items': items, 'matched': len(items),
                'scanned': scanned, 'pages_requested': attempted_pages,
                'pages_failed': failed_pages, 'total_reported': total_reported,
                # 실제 응답에는 중앙부처와 지자체 제공 서비스가 함께 들어온다.
                'source_scope': 'bokjiro_service_search',
                'provider_scope': 'all',
                'saved': False, 'ai_called': False}
    finally:
        try:
            if eng is not None:
                await eng.close()
        finally:
            _release_scrape_lock(handle)


async def collect_infant_services(max_pages: Optional[int] = None,
                                  progress_callback=None) -> List[Dict[str, Any]]:
    """복지로 중앙부처 서비스 전체를 페이징하며 '영유아' 태그 항목만 수집.

    반환: history_manager.save_data 호환 item dict 리스트.
    max_pages: 테스트용 상한(None=전체). progress_callback(done, total) 선택.
    """
    from scraper_engine import ScraperEngine

    eng = ScraperEngine(headless=True)
    await eng.start()
    items: List[Dict[str, Any]] = []
    seen = set()
    try:
        ctx = await new_public_context(eng.browser)
        page = await ctx.new_page()
        await page.goto(LIST_PAGE, wait_until="networkidle", timeout=40000)

        # 1페이지로 총건수 파악
        r = await page.request.post(API, data=_build_body(1), headers={"Content-Type": "application/json"})
        d = await r.json()
        total = (d.get("dmCount") or {}).get("dsServiceList0Count", 0)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if max_pages:
            total_pages = min(total_pages, max_pages)
        logger.info(f"[복지로 수집] 전체 {total}건 / {total_pages}페이지 스캔 시작")

        now = datetime.datetime.now().isoformat()
        for p in range(1, total_pages + 1):
            try:
                if p > 1:
                    r = await page.request.post(API, data=_build_body(p), headers={"Content-Type": "application/json"})
                    d = await r.json()
                for row in d.get("dsServiceList0", []):
                    rs = _parse_return_str(row.get("RETURN_STR", ""))
                    cyc = rs.get("BKJR_LFTM_CYC_CD", "")
                    title_tmp = (row.get("WLFARE_INFO_NM") or "").strip()
                    # 영유아-중심만 통과 (다생애주기 광범위·성인전용 노이즈 배제)
                    if not _is_infant_focused(cyc, title_tmp):
                        continue
                    wid = row.get("WLFARE_INFO_ID")
                    if not wid or wid in seen:
                        continue
                    seen.add(wid)
                    title = (row.get("WLFARE_INFO_NM") or "").strip()
                    desc = (row.get("WLFARE_INFO_OUTL_CN") or "").strip()
                    link = DETAIL + wid
                    # 지자체 동명 정책 구분: 담당기관/주소에서 지역 추출 → 제목 접두사 + residence
                    label, residence = _extract_region(
                        row.get("BIZ_CHR_INST_NM", ""), row.get("ADDR", ""))
                    # 타지역 지자체 정책(도봉 주민 무관)은 제외 — 전국/서울/도봉/강북/노원만
                    if not _is_target_region(residence):
                        continue
                    disp_title = f"[{label}] {title}" if label else title
                    items.append({
                        "site_name": "복지로(영유아)",
                        "title": disp_title,
                        "link": link,
                        "category": _map_category(rs.get("INTRS_THEMA_CD", "")),
                        "target": "영유아",
                        "residence": residence,
                        "contact": (row.get("BIZ_CHR_INST_NM") or "").strip(),  # 담당기관 보존
                        "support_detail": desc,
                        "matched_keyword": "복지로영유아",
                        "scraped_at": now,
                        "content_hash": hashlib.md5((title + desc).encode("utf-8")).hexdigest(),
                    })
            except Exception as e:
                logger.warning(f"[복지로 수집] page {p} 실패: {e}")
            if progress_callback and p % 20 == 0:
                progress_callback(p, total_pages)
        logger.info(f"[복지로 수집] 영유아 {len(items)}건 추출 (스캔 {total_pages}p)")
    finally:
        await eng.close()
    return items


# ── 상세 본문 보강 (지원대상/내용/신청방법/문의처) ───────────────────────────
# 복지로 상세 페이지(moveTWAT52011M)는 Nexacro류 SPA라 안정적 셀렉터가 없어
# 렌더 텍스트를 라벨 기준으로 파싱한다. 라벨 어휘는 실측으로 확정.
_DETAIL_LABELS = [
    "지원대상", "선정기준", "서비스 내용", "지원내용", "지원형태", "지원주기",
    "신청방법", "신청기간", "접수기관", "접수방법", "구비서류",
    "전화문의", "문의처", "근거법령", "서식/자료", "관련 웹사이트",
    "추가정보", "자주 묻는 질문", "제공유형",
]
_DETAIL_STOP = ("최종 수정일", "현재 페이지", "업무별 누리집", "의견 남기기")


def _parse_detail_text(text: str) -> Dict[str, str]:
    """렌더된 상세 텍스트에서 라벨별 본문을 추출."""
    if not text:
        return {}
    start = text.find("지원대상")
    if start < 0:
        return {}
    block = text[start:]
    for stop in _DETAIL_STOP:
        i = block.find(stop)
        if i > 0:
            block = block[:i]
    labelset = set(_DETAIL_LABELS)
    fields: Dict[str, list] = {}
    cur = None
    for ln in block.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s in labelset:           # 라벨 줄 → 새 필드 시작
            cur = s
            fields.setdefault(cur, [])
            continue
        if cur:
            s = _re_bk.sub(r'^[-•·]\s*', '', s)   # 글머리 기호 제거
            if s and s not in ("미리보기", "다운로드"):
                fields[cur].append(s)
    return {k: "\n".join(v).strip() for k, v in fields.items() if v}


def compose_detail(parsed: Dict[str, str]) -> Dict[str, str]:
    """파싱 결과 → DB 필드(support_detail/eligibility_detail/contact) 조립."""
    elig = "\n".join(x for x in [parsed.get("지원대상"), parsed.get("선정기준")] if x).strip()
    content = parsed.get("서비스 내용") or parsed.get("지원내용") or ""
    parts = []
    if content:
        parts.append(content)
    if parsed.get("신청방법"):
        parts.append(f"【신청방법】 {parsed['신청방법']}")
    if parsed.get("신청기간"):
        parts.append(f"【신청기간】 {parsed['신청기간']}")
    if parsed.get("근거법령"):
        parts.append(f"【근거】 {parsed['근거법령']}")
    support = "\n".join(parts).strip() or elig
    contact = (parsed.get("전화문의") or parsed.get("문의처") or "").strip()
    return {
        "support_detail": support[:1800],
        "eligibility_detail": elig[:800],
        "contact": contact[:200],
    }

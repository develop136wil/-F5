import asyncio
import difflib
import hashlib
import html as _html_mod
import json
from services.ai_generation_safety import (
    AiResponseReviewRequired, VISION_OUTPUT_TOKENS, complete_response_text,
)
import os
import re as _re_mod
import time
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qs, urlsplit
from services.logger import setup_logger
from services.board_detail_links import (
    board_kind, materialize_board_links, article_in_board, list_page_in_board, next_list_page,
)
from playwright.async_api import async_playwright
from services.public_transport import public_get, new_public_context

logger = setup_logger(__name__)


class _VisionRateLimiter:
    """이 파일의 Gemini Vision 호출(_scrape_vision_list·capture_page_vision) 전용 슬라이딩
    윈도우 리미터.

    (버그 2026-07-27, GPT Codex 리뷰로 발견) 이 두 호출은 scraper_service.py의
    ai_rate_limiter/ocr_rate_limiter를 전혀 거치지 않고 Gemini를 직접 불렀다. 그 결과
    "분류8+OCR4=12/분, 3건 여유"라는 예산 설계에 Vision 몫이 아예 빠져 있어, 특히 병렬
    사이트 스크래핑(최대 3개 동시) 중 여러 사이트가 동시에 Vision을 호출하면 실제 부하가
    무료 등급 한도(15 RPM)를 넘겨 429가 반복 발생했다(실측 로그로 확인).
    scraper_service.GeminiRateLimiter를 그대로 재사용하고 싶지만 이 모듈을 임포트하면
    scraper_service→scraper_engine 임포트와 순환되므로, site_analyzer.py와 동일한 원칙대로
    이 파일 안에 독립 구현한다."""

    def __init__(self, max_calls: int = 3, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._lock: Optional[asyncio.Lock] = None
        self._call_times: list = []
        self._last_wait_log = 0.0

    async def acquire(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        while True:
            async with self._lock:
                now = time.monotonic()
                self._call_times = [t for t in self._call_times if now - t < self.period]
                if len(self._call_times) < self.max_calls:
                    self._call_times.append(now)
                    return
                wait_time = self.period - (now - self._call_times[0]) + 0.5
            if wait_time > 0:
                if now - self._last_wait_log > 5.0:
                    self._last_wait_log = now
                    logger.info(f"[Rate Limiter] Vision 분당 {self.max_calls}건 한도 도달 — 약 {wait_time:.0f}초 대기 후 재개")
                await asyncio.sleep(wait_time)


# 모듈 단위 싱글톤 — 이 파일의 모든 Vision 호출이 공유. 기본값(3/분)은 scraper_service의
# 분류6+OCR3=9/분과 합쳐 총 12/분으로, 무료 등급 실측 한도(15 RPM) 대비 3건 여유를 유지한다.
_vision_rate_limiter = _VisionRateLimiter(
    max_calls=int(os.getenv('VISION_RATE_MAX_CALLS', '3')),
    period=float(os.getenv('AI_RATE_PERIOD_SEC', '60.0')),
)

# ── 환경변수 기반 타임아웃 설정 ──────────────────────────────────────────────
# .env 또는 환경변수로 재정의 가능 (지연 로딩: 모듈 임포트 시점이 아닌 첫 사용 시 읽음)
_pw_timeouts_loaded = False
_PW_GOTO_TIMEOUT   = 30000
_PW_SELECT_TIMEOUT = 10000
_PW_VISION_TIMEOUT = 20000
_PW_START_TIMEOUT  = 15.0

def _safe_env_num(key: str, default, caster=int):
    """.env 값이 사람 손으로 잘못 입력돼도(예: '30000ms', 공백) 앱이 죽지 않고
    기본값으로 안전하게 폴백한다."""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return caster(raw.strip())
    except (ValueError, TypeError):
        logger.warning(f"환경변수 {key}='{raw}' 파싱 실패 — 기본값 {default} 사용")
        return default


def _load_pw_timeouts():
    global _pw_timeouts_loaded, _PW_GOTO_TIMEOUT, _PW_SELECT_TIMEOUT, _PW_VISION_TIMEOUT, _PW_START_TIMEOUT
    if _pw_timeouts_loaded:
        return
    _PW_GOTO_TIMEOUT   = _safe_env_num('PLAYWRIGHT_GOTO_TIMEOUT_MS', 30000)
    _PW_SELECT_TIMEOUT = _safe_env_num('PLAYWRIGHT_SELECTOR_TIMEOUT_MS', 10000)
    _PW_VISION_TIMEOUT = _safe_env_num('PLAYWRIGHT_VISION_TIMEOUT_MS', 20000)
    _PW_START_TIMEOUT  = _safe_env_num('PLAYWRIGHT_START_TIMEOUT_SEC', 15.0, caster=float)
    _pw_timeouts_loaded = True

# ── Playwright 내장 객체 보호 스크립트 ────────────────────────────────────────
# 일부 공공기관 사이트가 IE 호환 폴리필로 Map/Set 등을 비표준 구현으로 교체함.
# page.evaluate() 호출 시 Playwright 내부에서 refs = new Map() + refs.set(...)을 사용하는데,
# 사이트 스크립트가 Map을 덮어쓰면 refs.set is not a function 오류 발생.
# add_init_script()로 페이지 자체 JS보다 먼저 실행하여 내장 객체를 잠금 처리.
_INIT_SCRIPT_PROTECT_GLOBALS = """
(() => {
    for (const name of ['Map', 'Set', 'WeakMap', 'WeakSet']) {
        try {
            const native = window[name];
            Object.defineProperty(window, name, {
                value: native,
                writable: false,
                configurable: false
            });
        } catch(e) {}
    }
})();
"""

import re as _re_title

# 제목에서 메뉴 경로(브레드크럼) 접두사를 제거하기 위한 패턴/네비 노드
_BREADCRUMB_RE = _re_title.compile(r'\s*[>》＞<＜]\s*')  # '>' / '<' 양쪽 브레드크럼
_NAV_ONLY_TITLES = {
    '공지사항', '공지', '새소식', '새소식/공지', '홍보·알림', '홍보알림', '알림마당',
    '정보마당', '사이버상담', '사이트정보', '자주하는 질문', '자주하는질문', 'faq',
    '신고센터', '목록', '홈', '전체', '보도자료', '보도자료/해명', '게시판',
    '회원서비스', '로그인', '회원가입',
    # NHIS 등 사이트 섹션/카테고리 명이 항목으로 잡힌 잡항목
    '건강모아', '정책·제도', '정책제도', '국민소통·참여', '국민소통참여',
    '정보공개', '국민참여', '알림·소식', '알림소식',
}
# 사이트 홈/슬로건/로그인 기본 title (실제 글 아님) — 수집 제외
_HOMEPAGE_JUNK = {
    '힘이 되는 평생 친구, 보건복지부', '가족센터', '임신육아종합포털 아이사랑',
    '국민건강보험', '복지로', '도봉구청', '서울특별시교육청',
}
# 로그인/인증 리다이렉트 페이지 title 감지
_LOGIN_TITLE_RE = _re_title.compile(r'로그인|sign\s*in|log\s*in', _re_title.IGNORECASE)


def clean_title(title: str) -> str:
    """스크랩 제목에서 메뉴 경로(브레드크럼) 접두사를 제거해 실제 제목만 남긴다.

    예) '열린행정>알림마당>보도자료/해명 - 도봉구, 유아 녹색소비 체험(상세보기)'
        → '도봉구, 유아 녹색소비 체험'
        '서울특별시교육청 : ... > 공지사항' → '공지사항'(→ nav면 후속 단계서 드롭)
        '보건사업>모자보건>미숙아 의료비 지원' → '미숙아 의료비 지원'
    """
    if not title:
        return title
    t = _re_title.sub(r'\s+', ' ', str(title).replace('\r', ' ').replace('\n', ' ')).strip()
    t = _re_title.sub(r'^(새글|NEW|new)\s+', '', t).strip()   # 목록 '새글' 마커 제거
    if '|' in t:                         # '제목 | 사이트명' → '|' 앞 본문만
        head = t.split('|', 1)[0].strip()
        if head:
            t = head
    if ' : ' in t:                       # '사이트명 : 경로 > ...' → ' : ' 뒤
        t = t.split(' : ', 1)[1].strip()
    if ('>' in t or '<' in t) and ' - ' in t:   # '경로 - 실제제목(상세보기)'
        after = t.split(' - ', 1)[1].strip()
        if after:
            t = after
    elif '>' in t or '<' in t:           # 마지막 경로 세그먼트
        seg = _BREADCRUMB_RE.split(t)[-1].strip()
        if seg:
            t = seg
    t = _re_title.sub(r'\(상세보기\)\s*$', '', t).strip()
    return t


def is_nav_only_title(title: str) -> bool:
    """순수 메뉴/네비/로그인/홈 title(실제 글 아님)이면 True → 수집 제외 대상."""
    raw = (title or '').strip()
    t = raw.lower()
    if not t:
        return True
    if t in {x.lower() for x in _NAV_ONLY_TITLES}:
        return True
    if raw in _HOMEPAGE_JUNK:
        return True
    # 게시판 목록/카테고리 페이지 (실제 글 아님): '... 게시판목록', '게시판 목록'
    if '게시판목록' in raw or '게시판 목록' in raw:
        return True
    # 로그인/회원 리다이렉트 페이지 (예: '로그인 < 회원서비스 < ... | 국민건강보험')
    if _LOGIN_TITLE_RE.search(raw) and ('회원' in raw or '인증' in raw or '<' in raw):
        return True
    return False


def _clean_items(items):
    """item 리스트의 title을 정리하고 순수 네비 항목은 제외."""
    out = []
    for it in items or []:
        # DOM-only selection hints never become stored source text or AI input.
        it = {key: value for key, value in it.items()
              if key not in ('_publication_text', '_article_context', '_in_navigation')}
        ct = clean_title(it.get('title', ''))
        if is_nav_only_title(ct):
            continue
        it['title'] = ct
        if not (it.get('link') or '').strip():
            # onclick형 게시판(dbwc2017 등)은 javascript:/# href가 걸러지면 link가 빈
            # 문자열로 남는다. welfare_items.link는 UNIQUE라서 빈 문자열 항목이 여러 개면
            # 저장 시 서로를 덮어써 마지막 1건만 남는 데이터 유실이 있었다(실측 2026-07-15).
            # (site_name, 제목) 기반 결정론적 고유값을 합성해 저장을 보존한다 — 재수집 시
            # 같은 제목이면 같은 값이 나와 기존 dedup(link 매칭)도 그대로 동작한다.
            # "about:blank#..." 형태라 프론트 isRealLink 가드가 실제 클릭은 막아준다.
            site = it.get('site_name') or 'unknown'
            h = hashlib.sha1(f"{site}|{ct}".encode('utf-8')).hexdigest()[:12]
            it['link'] = f"about:blank#{site}-{h}"
        out.append(it)
    return out

def _filter_article_links(raw_links, site_config):
    """Keep whole-page fallback inside the registered board, not partner sites.

    News applies this boundary to explicit selectors as well as fallback links.
    Legacy non-News readers keep their existing explicit-selector behavior.
    """
    def host(value):
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
                return ''
            return (parsed.hostname or '').lower().removeprefix('www.')
        except (TypeError, ValueError):
            return ''

    allowed = {host(site_config.get('url', ''))}
    extra_hosts = site_config.get('allowed_article_hosts') or []
    if isinstance(extra_hosts, (list, tuple)):
        for value in extra_hosts:
            if isinstance(value, str) and value and '/' not in value and '*' not in value:
                allowed.add(host('https://' + value))
    allowed.discard('')
    detail_keys = {'articleno', 'nttid', 'nttsn', 'bbssn', 'boardseq', 'seq', 'idx',
                   'wr_id', 'article_seq', 'bidx', 'bcidx', 'datasid', 'board_seq',
                   'board_no', 'b_idx', 'data_no', 'postid', 'post_id', 'bbscttseq'}
    seen, found = set(), []
    for raw in raw_links or []:
        link = raw.get('link', '')
        if host(link) not in allowed or raw.get('_in_navigation') is True or link in seen:
            continue
        parsed = urlsplit(link)
        query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
        detail = (bool(detail_keys.intersection(query)) or
                  any(value.lower() in ('view', 'read', 'detail')
                      for key in ('mode', 'act', 'action', 'cmd') for value in query.get(key, [])) or
                  bool(_re_mod.search(r'/(?:view|read|detail|post|article)(?:[./_-]|$)|'
                                      r'/(?:board|notice|news)/[^/]+/\d+(?:/|$)', parsed.path, _re_mod.I)))
        from services.news_publication import from_labelled_text, from_list_item
        published = from_list_item(raw) or from_labelled_text(raw.get('_publication_text', ''))
        # Older custom readers without DOM metadata keep same-host compatibility.
        if '_article_context' in raw and not (detail or raw['_article_context']):
            continue
        hash_article = (parsed.fragment.startswith(('/', '!/'))
                        and parsed.fragment not in ('/', '!/')
                        and raw.get('_article_context') is True)
        if parsed.path in ('', '/') and not detail and not hash_article:
            continue
        if site_config.get('news_article_only'):
            if not article_in_board(site_config.get('url', ''), link):
                continue
            # Exact generic menu labels are not posts. An arbitrary table row
            # must not turn a static section URL into a news announcement.
            title = clean_title(raw.get('title', ''))
            if is_nav_only_title(title):
                continue
            static_path = bool(_re_mod.search(r'/(?:subMenu|content|contents|intro|menu)(?:[./_-]|$)',
                                             parsed.path, _re_mod.I))
            static_title = bool(_re_mod.search(r'(?:정책의?\s*이해|서비스|기관\s*소개|조직도|오시는\s*길|공공요금\s*감면)\s*(?:상세\s*보기)?$', title))
            announcement = bool(_re_mod.search(r'공고|모집|접수|신청|개최|변경|시행|안내문|결과|\b20\d{2}\b', title))
            if not detail and not published and not announcement and (static_path or static_title):
                continue
        seen.add(link)
        item = {key: value for key, value in raw.items() if not key.startswith('_')}
        if published and site_config.get('news_article_only'):
            item['published_date'] = published
        found.append(item)
    return found


class ScraperEngine:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser = None

    async def start(self) -> None:
        """Starts the playwright instance and browser."""
        if self.browser:
            return  # 이미 시작됨
        _load_pw_timeouts()
        logger.info("Initializing Playwright Engine...")

        # Prevent indefinite hang during PyInstaller execution
        try:
            self.playwright = await asyncio.wait_for(async_playwright().start(), timeout=_PW_START_TIMEOUT)
            try:
                self.browser = await self.playwright.chromium.launch(headless=self.headless, channel="msedge")
            except Exception as e:
                logger.warning(f"MSEdge not found ({e}), falling back to Chromium...")
                try:
                    self.browser = await self.playwright.chromium.launch(headless=self.headless)
                except Exception:
                    await self._install_browser()
                    self.browser = await self.playwright.chromium.launch(headless=self.headless)
        except Exception as e:
            # (버그 2026-07-23) 브라우저 실행에 실패했는데 playwright 드라이버는 이미
            # 떠 있으면, 여기서 정리하지 않으면 그 서브프로세스가 그대로 누수된다. 호출부가
            # start() 성공을 전제로 close()를 finally에 두는 경우가 많아, start() 스스로
            # 실패 시 정리하는 게 안전하다(site_analyzer.start()와 동일한 원칙).
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            self.browser = None
            raise RuntimeError(f"Browser launch failed: {e}") from e

    @staticmethod
    async def _install_browser() -> None:
        """Auto-install Chromium if not found (first-run setup)."""
        import subprocess
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        logger.info("🔧 Installing Playwright Chromium (first-time setup)...")
        process = None
        try:
            env = get_driver_env()
            driver_executable, driver_cli = compute_driver_executable()
            process = await asyncio.create_subprocess_exec(
                driver_executable, driver_cli, "install", "chromium",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {})
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                logger.info("✅ Chromium installed successfully")
            else:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(f"❌ Chromium install failed: {error_msg}")
                raise RuntimeError(f"Failed to install Chromium: {error_msg}")
        except asyncio.CancelledError:
            # A cancelled diagnostic must not leave its installer running.
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except TimeoutError:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=3)
                except ProcessLookupError:
                    pass
            raise
        except Exception as e:
            logger.error(f"Playwright install error: {e}")
            raise

    async def _scrape_link_based(self, page, site_config: Dict[str, Any], *, strict=False) -> List[Dict[str, Any]]:
        """
        링크 기반 추출 (1단계): 페이지 내 <a> 태그 전체를 순회하여
        텍스트 8자 이상인 링크를 항목으로 반환. API 비용 없음.
        """
        name = site_config['name']
        base_url = site_config.get('base_url', '')

        try:
            raw_links = await page.evaluate(r'''
                ({baseUrl, programCards, knownBoard}) => {
                    const _NAV_BLACKLIST = new Set([
                        '바로가기','더보기','자세히보기','자세히 보기','클릭','이동','보기','확인',
                        '메뉴 바로가기','본문 바로가기','본문으로','콘텐츠 바로가기',
                        '새창','새 창','닫기','열기','이전','다음','목록','홈','처음으로',
                        '이전페이지','다음페이지','top','맨위로',
                    ]);
                    const results = [];
                    const anchors = document.querySelectorAll('a[href], a[onclick]');
                    for (const a of anchors) {
                        // This one known board puts the title next to a short
                        // '자세히보기' link. Python validates the exact board URL.
                        const cardTitle = programCards ? a.closest('.item')?.querySelector('.cont > .tit')?.textContent?.trim() : '';
                        let anchorTitle = (a.innerText || a.textContent || '').trim();
                        if (knownBoard) {
                            const titleCopy = a.cloneNode(true);
                            titleCopy.querySelectorAll('.bbs-ico').forEach(badge => badge.remove());
                            anchorTitle = (titleCopy.textContent || '').trim().replace(/\s+/g, ' ');
                        }
                        const text = cardTitle || anchorTitle;
                        // Preserve short announcement titles; scope checks below remove navigation.
                        if (text.length < 8 || text.length > 200) continue;
                        if (_NAV_BLACKLIST.has(text)) continue;

                        let href = a.getAttribute('href') || '';
                        const rawHref = href;
                        const scriptLink = !href || href.startsWith('#') || /^\s*javascript:/i.test(href);

                        // 로그인/회원 관련 URL 제외 (서버 리다이렉트 방지)
                        const _LOGIN_RE = /\/(login|signin|logIn|member\/login|auth\/login|회원가입|join)/i;
                        if (_LOGIN_RE.test(href)) continue;

                        // 파일 다운로드 URL 제외 (첨부파일 직접 링크)
                        const _DL_RE = /[?&]mode=download|[?&]attachNo=|\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|egg)([?&#]|$)/i;
                        if (_DL_RE.test(href)) continue;

                        // 페이지네이션/목록 URL 제외 (article.offset, pageNum 등)
                        // 단, 상세 식별자(articleNo, mode=view, seq 등)가 있으면 실제 기사이므로 유지.
                        // (NHIS 기사 URL은 ?mode=view&articleNo=X&article.offset=0&articleLimit=10 형태라
                        //  순수 페이지네이션으로 오인돼 잘려나가던 버그 수정)
                        const _DETAIL_RE = /[?&](articleNo|nttId|nttSn|bbsSn|boardSeq|seq|idx|wr_id|article_seq|bIdx|bcIdx|dataSid|board_seq)=|[?&](mode|act|action)=(view|read|detail)([&#]|$)/i;
                        const _PAGE_RE = /[?&](article\.offset|articleLimit|pageNo|pageNum|currentPage|listPage|page=\d|offset=\d)/i;
                        if (_PAGE_RE.test(href) && !_DETAIL_RE.test(href)) continue;

                        if (!scriptLink && !href.startsWith('http')) {
                            // 표준 URL 결합: 쿼리상대(?mode=view), 경로상대, 루트상대 모두
                            // 현재 페이지 URL 기준으로 정확히 해석 (문자열 이어붙이기 금지).
                            // 예) base가 도메인루트면 '?mode=view'가 .do 게시판경로를 잃어 빈 페이지가 되던 NHIS 버그 수정.
                            try {
                                href = new URL(href, window.location.href).href;
                            } catch(e) { continue; }
                        }

                        // An article's own header contains its title, unlike the
                        // page-wide header. Check all ancestors for page chrome.
                        let header = a.closest('header');
                        let pageHeader = false;
                        while (header) {
                            if (!header.closest('article')) {
                                pageHeader = true;
                                break;
                            }
                            header = header.parentElement && header.parentElement.closest('header');
                        }
                        const navigation = pageHeader || a.closest('nav, footer, [role="navigation"], '
                            + '#header, #footer, #gnb, #lnb, #snb, .gnb, .lnb, .snb, .breadcrumb, '
                            + '.breadcrumbs, .banner, .partners, .family-site, .family_site');
                        const articleContext = a.closest('tr, article, [class*="board"], [id*="board"], '
                            + '[class*="bbs"], [id*="bbs"], [class*="notice"], [id*="notice"]');
                        const listItem = a.closest('li');
                        const datedItem = listItem && (/\b20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}\b/.test(listItem.innerText || '')
                            || listItem.querySelector('time, .date, .time'));
                        const row = a.closest('tr, li, article');
                        let publicationText = row ? (row.innerText || '') : '';
                        const publishedTime = row && row.querySelector('[itemprop="datePublished"], time[pubdate]');
                        if (publishedTime) publicationText = '게시일: ' + (publishedTime.getAttribute('datetime') || publishedTime.getAttribute('content') || publishedTime.textContent || '');
                        if (row && row.tagName === 'TR') {
                            const headers = row.closest('table')?.querySelectorAll('thead th') || [];
                            for (const cell of row.querySelectorAll('td')) {
                                const label = headers[cell.cellIndex]?.textContent || '';
                                if (/게시일|등록일|작성일|발행일/.test(label) && !/마감|신청|접수|수정/.test(label))
                                    publicationText += '\n' + label + ': ' + (cell.textContent || '');
                            }
                        }
                        results.push({ title: text, link: scriptLink ? '' : href, date: '',
                            _board_page: window.location.href, _board_script: scriptLink,
                            _board_href: rawHref, _board_onclick: a.getAttribute('onclick') || '',
                            _board_card: !!cardTitle && !scriptLink,
                            _publication_text: publicationText,
                            _in_navigation: !!navigation, _article_context: !!(articleContext || datedItem) });
                    }
                    // Deduplicate after Python's scope filter: a menu occurrence
                    // must not hide the same URL's later real article occurrence.
                    return results;
                }
            ''', {'baseUrl': base_url, 'programCards': board_kind(site_config.get('url')) == 'program',
                  'knownBoard': bool(board_kind(site_config.get('url')))})

            raw_links = materialize_board_links(raw_links, site_config.get('url'))
            selected_links = _filter_article_links(raw_links, site_config)
            items = [{'site_name': name, **r} for r in selected_links]
            excluded = len(raw_links) - len(selected_links)
            if excluded:
                logger.info(f"   -> {name}: 메뉴·외부기관 등 게시글 범위 밖 링크 {excluded}개 제외")
            logger.info(f"   -> {name}: 링크 기반 {len(items)}개 추출")
            return items

        except Exception as e:
            logger.error(f"Link-based scrape failed for {name}: {e}")
            if strict:
                raise
            return []

    async def _scrape_vision_list(self, page, site_config: Dict[str, Any], *,
                                  privacy_reviewed=False) -> List[Dict[str, Any]]:
        """
        Vision 기반 목록 추출 (2단계): Playwright 스크린샷 → Gemini Vision (기본 3.5 Flash-Lite).
        네비게이션/푸터를 제외한 실제 게시글 제목+날짜를 추출.
        링크 정보는 없으므로 _scrape_link_based 결과와 병합해서 사용.
        AI 응답 미완료 시 검토 예외를 전달한다. 빈 목록으로 성공 처리하지 않는다.
        """
        # A persistent site setting or a truthy string is not per-call consent.
        # Check before capturing bytes, constructing a client or reserving quota.
        if privacy_reviewed is not True:
            logger.info('화면 이미지의 AI 추가 분석은 직접 확인 전까지 보류합니다.')
            return []
        name = site_config['name']
        key = os.getenv('GOOGLE_API_KEY')
        if not key:
            return []

        try:
            from google import genai
            from google.genai import types

            screenshot_bytes = await page.screenshot(type='jpeg', quality=75)
            from services import ai_coordinator
            client = ai_coordinator.create_client(key)

            await _vision_rate_limiter.acquire()

            prompt = (
                "이 복지/공공기관 웹페이지에서 실제 공고·프로그램 게시글 목록만 추출해주세요.\n"
                "제외 대상: 메뉴, 네비게이션, 버튼, 배너, 푸터, 사이드바, 검색창.\n"
                "반환 형식 (JSON 배열만):\n"
                '[{"title": "공고 제목", "date": "날짜 (없으면 빈 문자열)"}]\n'
                "공고가 없으면 [] 반환. JSON 외 텍스트는 포함하지 마세요."
            )

            model_name = os.getenv('OCR_MODEL') or os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
            await ai_coordinator.acquire_model_slot(model_name,
                input_tokens=ai_coordinator.estimate_input_tokens(prompt, media_bytes=len(screenshot_bytes)))
            response = await ai_coordinator.await_api(client.aio.models.generate_content(
                # OCR과 같은 성격이므로 OCR_MODEL→CLASSIFY_MODEL 순으로 따른다.
                # 제공사 한도는 프로젝트·모델별 응답을 따르며 분당 요청을 조절한다.
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=screenshot_bytes, mime_type='image/jpeg'),
                    types.Part.from_text(text=prompt),
                ],
                config=types.GenerateContentConfig(response_mime_type='application/json',
                    temperature=0, max_output_tokens=VISION_OUTPUT_TOKENS, candidate_count=1),
            ))

            # Gemini가 마크다운 코드블록으로 감싸 반환하는 경우 JSON 추출
            response_text = complete_response_text(response)
            if response_text.startswith("```"):
                import re as _re
                match = _re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
                response_text = match.group(1).strip() if match else response_text
            try:
                raw = json.loads(response_text)
            except json.JSONDecodeError:
                raise AiResponseReviewRequired('incomplete') from None
            if not isinstance(raw, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get('title'), str)
                    or not item['title'].strip()
                    or not isinstance(item.get('date', ''), str) for item in raw):
                raise AiResponseReviewRequired('incomplete')
            items = [
                {'title': i['title'].strip(), 'date': i.get('date', '')}
                for i in raw if i.get('title', '').strip()
            ]
            logger.info(f"   -> {name}: Vision이 {len(items)}개 항목 감지")
            return items

        except AiResponseReviewRequired as error:
            logger.warning('Vision list response review required: %s', error.reason)
            raise
        except Exception as e:
            try:
                ai_coordinator.register_api_error(model_name, e)
            except (NameError, UnboundLocalError):
                pass
            logger.error(f"Vision list fallback failed for {name}: {e}")
            return []

    def _merge_link_vision(
        self,
        link_items: List[Dict[str, Any]],
        vision_items: List[Dict[str, Any]],
        site_config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Vision 감지 항목과 링크 기반 항목을 제목 유사도로 매칭하여 병합.
        - 매칭되면: Vision 제목(깔끔) + 링크 기반 href 사용
        - 매칭 없으면: Vision 항목을 link='' 으로 추가 (제목 기반 분류만 가능)
        """
        name = site_config['name']
        if not vision_items:
            return link_items

        link_titles = [item['title'] for item in link_items]
        merged: List[Dict[str, Any]] = []
        used_link_indices: set = set()

        for v_item in vision_items:
            v_title = v_item['title']
            best_score = 0.0
            best_idx = -1

            for idx, l_title in enumerate(link_titles):
                if idx in used_link_indices:
                    continue
                score = difflib.SequenceMatcher(None, v_title, l_title).ratio()
                if score > best_score:
                    best_score = score
                    best_idx = idx

            if best_idx >= 0 and best_score >= 0.35:
                used_link_indices.add(best_idx)
                merged_item = link_items[best_idx].copy()
                merged_item['title'] = v_title  # Vision 제목이 더 깔끔
                if v_item.get('date') and not merged_item.get('date'):
                    merged_item['date'] = v_item['date']
                merged.append(merged_item)
            else:
                # 링크 없이 추가 — 키워드 필터 + 제목 기반 분류만 가능
                merged.append({
                    'site_name': name,
                    'title': v_title,
                    'date': v_item.get('date', ''),
                    'link': '',
                })

        matched = len(used_link_indices)
        logger.info(f"   -> {name}: 하이브리드 병합 완료 — {len(merged)}개 (링크 매칭 {matched}개)")
        return merged

    async def _enrich_titles(self, items: List[Dict]) -> List[Dict]:
        """
        링크 기반 추출 항목의 앵커 텍스트 제목을 실제 페이지 <title> 태그로 보강.
        - aiohttp로 동시 10개 요청, 페이지당 5초 타임아웃
        - 첫 8KB만 읽어 title 태그 추출 (전체 다운로드 방지)
        - HTML 엔티티 디코딩 (&amp; 등)
        - 취득 실패 또는 빈 title인 경우 원래 앵커 텍스트 유지
        """
        import aiohttp

        if not items:
            return items

        _title_re = _re_mod.compile(r'<title[^>]*>([^<]{1,300})</title>', _re_mod.IGNORECASE)
        _sem = asyncio.Semaphore(10)
        _ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

        async def _fetch(session: aiohttp.ClientSession, item: Dict) -> Dict:
            link = item.get('link', '')
            if not link or not link.startswith('http'):
                return item
            async with _sem:
                try:
                    async with public_get(session, link, allow_redirects=True) as resp:
                        if resp.status == 200:
                            # 첫 8KB만 읽음 — <title>은 항상 <head> 안에 있음
                            chunk = await resp.content.read(8192)
                            charset = resp.charset or 'utf-8'
                            snippet = chunk.decode(charset, errors='replace')
                            m = _title_re.search(snippet)
                            if m:
                                fetched = _html_mod.unescape(m.group(1).strip())
                                if fetched:
                                    return {**item, 'title': fetched}
                except Exception:
                    pass
            return item

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={'User-Agent': _ua},
        ) as session:
            enriched = list(await asyncio.gather(*[_fetch(session, i) for i in items]))

        updated = sum(1 for o, n in zip(items, enriched) if o.get('title') != n.get('title'))
        if updated:
            logger.info(f"   -> 제목 보강: {updated}/{len(enriched)}개 업데이트")
        return enriched

    async def _scrape_fallback(self, page, site_config: Dict[str, Any], *, strict=False,
                               review_status=None) -> List[Dict[str, Any]]:
        """
        하이브리드 Fallback 오케스트레이터 (셀렉터 없는 사이트용).
          1단계: 링크 기반 추출 (무료, 빠름)
          1.5단계: 각 링크 페이지 <title> 태그로 제목 보강
          2단계: 결과가 VISION_THRESHOLD 미만이면 화면 이미지의 직접 확인 필요 표시
        """
        # Link scarcity can mean an image/JS-only list, not genuinely zero posts.
        # Never send screenshots automatically; retain the available link items.
        # 환경변수 VISION_THRESHOLD로 조정 가능 (기본 10).
        VISION_THRESHOLD = int(os.getenv('VISION_THRESHOLD', '10'))
        name = site_config['name']
        logger.info(f"   -> {name}: 셀렉터 없음, 하이브리드 fallback 시작")

        link_items = await self._scrape_link_based(page, site_config, strict=strict)
        # These known boards expose exact post titles in the list, but their
        # HTML <title> is only the institution/section name. Keep the post title
        # for registration's title-in-body check and avoid redundant requests.
        if (link_items and site_config.get('fallback_enrich_titles', True)
                and not board_kind(site_config.get('url'))):
            link_items = await self._enrich_titles(link_items)

        if len(link_items) >= VISION_THRESHOLD:
            logger.info(f"   -> {name}: 링크 {len(link_items)}개 충분 — Vision 생략")
            return link_items

        logger.info(f"   -> {name}: 링크 {len(link_items)}개, 화면 이미지의 추가 분석은 직접 확인 필요")
        if review_status is not None:
            review_status['vision_review_required'] = True
        return link_items

    async def _advance_news_page(self, page, site_config, visited):
        """Read pager data and follow one same-board GET, never click page JS."""
        snapshot = await page.evaluate(r'''() => {
            const pager = '.pagination,.paging,.paginate,.pagenation,.page,.page_num,.bbs_paging,#pagination,[aria-label*="pagination"],[aria-label*="페이지"]';
            const controls = [];
            for (const el of document.querySelectorAll('a,button,span[aria-current]')) {
                const text = (el.getAttribute('aria-label') || el.getAttribute('title') || el.querySelector('img[alt]')?.getAttribute('alt') || el.textContent || '').trim();
                const relNext = (el.getAttribute('rel') || '').split(/\s+/).includes('next');
                const next = relNext || /^(?:다음(?:\s*\d+)?(?:\s*페이지)?(?:\s*이동)?|next(?:\s*page)?|[>›→])$/i.test(text);
                if (!el.closest(pager) && !relNext && !(next && el.matches('a.next,a.btn-next'))) continue;
                if (!next && !/^\d{1,7}$/.test(text)) continue;
                controls.push({label:text, next, href:el.getAttribute('href') || '',
                    onclick:el.getAttribute('onclick') || '',
                    current:el.getAttribute('aria-current') === 'page' || /(?:^|\s)(?:active|current|on)(?:\s|$)/.test(el.className || ''),
                    disabled:el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true' || /(?:^|\s)disabled(?:\s|$)/.test(el.className || '')});
                if (controls.length >= 200) break;
            }
            return {url:window.location.href, controls};
        }''')
        if not isinstance(snapshot, dict):
            return 'unverified'
        chosen = next_list_page(site_config['url'], snapshot.get('url'), snapshot.get('controls'))
        if chosen['state'] != 'next':
            return chosen['state']
        target = chosen['url']
        if target in visited:
            return 'repeated'
        # Redirects cannot turn a validated next link into another board visit.
        async def guard(route):
            request = route.request
            if request.is_navigation_request() and (request.method != 'GET'
                    or not list_page_in_board(site_config['url'], request.url)):
                await route.abort()
            else:
                await route.fallback()
        await page.route('**/*', guard)
        try:
            response = await page.goto(target, wait_until='domcontentloaded', timeout=_PW_GOTO_TIMEOUT)
            if response is not None and response.status >= 400:
                return 'failed'
            if not list_page_in_board(site_config['url'], page.url):
                return 'unverified'
            visited.add(target)
            return 'next'
        finally:
            await page.unroute('**/*', guard)

    async def _scrape_news_fallback_pages(self, page, site_config, *, strict, review_status):
        """Respect the user's page cap; keep earlier results on a later failure."""
        raw_limit = site_config.get('max_pages', 1)
        limit = raw_limit if type(raw_limit) is int and 1 <= raw_limit <= 100 else 1
        items, seen, pages = [], set(), set()
        visited = {site_config['url']}
        status = 'page_limit'
        for index in range(limit):
            try:
                rows = await self._scrape_fallback(page, site_config, strict=strict, review_status=review_status)
                identity = tuple(sorted(row.get('link', '') for row in rows))
                if index and (not identity or identity in pages):
                    status = 'repeated' if identity else 'unverified'
                    break
                pages.add(identity)
                for row in rows:
                    if row.get('link') not in seen:
                        seen.add(row.get('link'))
                        items.append(row)
                if index + 1 >= limit:
                    break
                status = await self._advance_news_page(page, site_config, visited)
                if status != 'next':
                    break
                status = 'page_limit'
            except asyncio.CancelledError:
                raise
            except Exception:
                status = 'failed'
                break
        return items, status

    async def capture_page_vision(self, url: str, *, privacy_reviewed=False) -> str:
        """
        상세 페이지 하이브리드 콘텐츠 추출:
        Playwright 스크린샷 → Gemini Vision (기본 3.5 Flash-Lite).
        텍스트가 부족한 JS 렌더링 페이지에서 복지 정보를 Notion 마크다운으로 반환.
        AI 응답 미완료 시 검토 예외를 전달하며 브라우저 컨텍스트는 닫는다.
        """
        if privacy_reviewed is not True:
            logger.info('상세 화면 이미지의 AI 분석은 직접 확인 전까지 보류합니다.')
            return ''
        if not self.browser or not url:
            return ''

        key = os.getenv('GOOGLE_API_KEY')
        if not key:
            return ''

        context = await new_public_context(self.browser,
            ignore_https_errors=True,
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
            ),
        )
        page = await context.new_page()
        await page.add_init_script(_INIT_SCRIPT_PROTECT_GLOBALS)
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=_PW_VISION_TIMEOUT)
            await asyncio.sleep(1)
            screenshot_bytes = await page.screenshot(type='jpeg', quality=75)

            from google import genai
            from google.genai import types

            from services import ai_coordinator
            client = ai_coordinator.create_client(key)
            await _vision_rate_limiter.acquire()
            prompt = (
                "이 복지 서비스/지원 사업 공고 페이지에서 핵심 정보를 추출하여 "
                "Notion에 바로 붙여넣기 좋은 마크다운 형식으로 정리해주세요.\n\n"
                "아래 섹션 중 해당 정보가 있는 것만 포함하세요:\n\n"
                "### ✅ 지원 내용\n- ...\n\n"
                "### 📋 신청 자격\n- ...\n\n"
                "### 📍 신청 방법\n- ...\n\n"
                "### 💰 지원 금액 및 혜택\n- ...\n\n"
                "### 📅 신청 기간\n- 시작일 ~ 마감일\n\n"
                "### 📞 문의처\n- ...\n\n"
                "없는 섹션은 생략하고, 한국어로 작성하세요. 마크다운 텍스트만 반환하세요."
            )

            model_name = os.getenv('OCR_MODEL') or os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
            await ai_coordinator.acquire_model_slot(model_name,
                input_tokens=ai_coordinator.estimate_input_tokens(prompt, media_bytes=len(screenshot_bytes)))
            response = await ai_coordinator.await_api(client.aio.models.generate_content(
                # 위와 같은 모델 선택 및 공통 요청 상한을 사용한다.
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=screenshot_bytes, mime_type='image/jpeg'),
                    types.Part.from_text(text=prompt),
                ],
                config=types.GenerateContentConfig(temperature=0,
                    max_output_tokens=VISION_OUTPUT_TOKENS, candidate_count=1),
            ))
            result = complete_response_text(response)
            logger.info(f"   -> Vision 상세 분석 완료: {url[:60]}...")
            return result

        except AiResponseReviewRequired as error:
            logger.warning('Vision detail response review required: %s', error.reason)
            raise
        except Exception as e:
            try:
                ai_coordinator.register_api_error(model_name, e)
            except (NameError, UnboundLocalError):
                pass
            logger.error(f"capture_page_vision failed for {url}: {e}")
            return ''
        finally:
            await context.close()

    async def fetch_rendered_page(self, url: str, *, timeout_seconds: float = 12.0,
                                  url_validator=None, capture_images=False):
        """Bounded public-page recovery, with no cloud AI or permission bypass.

        Returns the ordinary page/attachment schema, or None on access failure.
        Static images normally stay on the cached attachment OCR path. A caller
        that already observed an image download failure may explicitly request
        one loaded poster capture. Both poster/canvas OCR stay entirely local.
        """
        if not self.browser or not url:
            return None
        from services.news_attachments import validate_public_attachment_url
        from services import page_coverage
        from ai_classifier import _parse_page_html
        validator = url_validator or validate_public_attachment_url
        budget = max(1.0, min(float(timeout_seconds), 15.0))
        context = None

        async def read_page():
            nonlocal context
            safe_url = await validator(url)
            context = await new_public_context(self.browser,
                ignore_https_errors=False, accept_downloads=False,
                service_workers='block', viewport={'width': 1200, 'height': 1600},
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'))
            validated = set()

            async def guard_request(route, request):
                target = request.url
                try:
                    parsed = urlsplit(target)
                    if parsed.scheme in ('data', 'blob'):
                        await route.fallback()
                        return
                    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                            or parsed.username or parsed.password or len(target) > 10000 or
                            request.method not in ('GET', 'HEAD') or request.resource_type == 'media'):
                        await route.abort('blockedbyclient')
                        return
                    origin = (parsed.scheme, parsed.hostname, parsed.port)
                    if origin not in validated:
                        await validator(target)
                        validated.add(origin)
                    await route.fallback()
                except Exception:
                    await route.abort('blockedbyclient')

            await context.route('**/*', guard_request)
            if hasattr(context, 'route_web_socket'):
                async def block_socket(socket):
                    await socket.close()
                await context.route_web_socket('**/*', block_socket)
            page = await context.new_page()
            await page.add_init_script(_INIT_SCRIPT_PROTECT_GLOBALS)
            response = await page.goto(safe_url, wait_until='domcontentloaded', timeout=int(budget * 1000))
            if response is not None and response.status >= 400:
                return None, None
            await validator(page.url)
            # A short DOM settle is enough; networkidle often never happens on
            # government pages with analytics/polling and used to cost 5s each.
            await page.wait_for_timeout(400)
            html = await page.content()
            if len(html) > 2_000_000:
                return None, None
            parsed = await asyncio.to_thread(_parse_page_html, html, page.url)
            title = str(parsed.get('title') or '').lower()
            opening = str(parsed.get('text') or '')[:1200].lower()
            challenge = ('captcha', 'access denied', '403 forbidden', 'verify you are human',
                         'checking your browser', '접근이 제한', '접근 권한이 없', '자동입력 방지')
            login_path = _re_mod.search(r'/(?:login|signin|auth)(?:[./?]|$)', page.url, _re_mod.I)
            if (login_path or any(term in title or term in opening for term in challenge) or
                    title.strip() in ('로그인', 'login', 'sign in')):
                return None, None
            # Include at most two public iframe bodies. Relative attachment links
            # must be parsed against each frame URL, never against the outer page.
            for frame in [f for f in page.frames if f is not page.main_frame][:2]:
                try:
                    await validator(frame.url)
                    frame_html = await frame.content()
                    if len(frame_html) > 2_000_000:
                        continue
                    extra = await asyncio.to_thread(_parse_page_html, frame_html, frame.url)
                    extra_text = str(extra.get('text') or '').strip()
                    if extra_text and extra_text not in parsed.get('text', ''):
                        for key in ('text', 'policy_text'):
                            merged = '\n'.join(filter(None, [parsed.get(key, ''), extra.get(key, '')]))
                            if len(merged) > 15000:
                                parsed['source_page'] = page_coverage.with_reasons(parsed.get('source_page'), 'page_char_limit')
                            parsed[key] = merged[:15000]
                    for key in ('images', 'attachments', 'attachment_items'):
                        original = parsed.setdefault(key, [])
                        original.extend(value for value in extra.get(key, []) if value not in original)
                    parsed['source_page'] = page_coverage.with_reasons(parsed.get('source_page'),
                        *extra.get('source_page', {}).get('reasons', []))
                except Exception:
                    # Preserve outer content and mark the inaccessible region.
                    parsed['source_page'] = page_coverage.with_reasons(parsed.get('source_page'), 'fetch_failed')
            parsed.update(final_url=page.url, content_type='text/html', rendered_locally=True)
            screenshot = None
            if capture_images is True and len(str(parsed.get('text') or '').strip()) < 120:
                posters = set(parsed.get('images') or [])
                if posters:
                    images = await page.query_selector_all(
                        'main img, article img, .bbsCont img, .board_view img, .board-view img, .view-content img')
                    for poster in images[:30]:
                        info = await poster.evaluate(r'''img => ({
                            src: img.currentSrc || img.src,
                            loaded: img.complete && img.naturalWidth >= 200 && img.naturalHeight >= 120,
                            excluded: !!img.closest('nav, header, footer, [role="navigation"]') ||
                                /captcha|자동.*방지|보안.*문자|로고|logo/i.test((img.alt || '') + ' ' + (img.src || ''))
                        })''')
                        if (not info.get('loaded') or info.get('excluded') or info.get('src') not in posters):
                            continue
                        box = await poster.bounding_box()
                        if (box and 200 <= box['width'] <= 2000 and 120 <= box['height'] <= 2000
                                and box['width'] * box['height'] <= 2_000_000):
                            screenshot = await poster.screenshot(type='png', timeout=1500)
                            parsed['_captured_poster_url'] = info['src']
                            break
            if (len(str(parsed.get('text') or '').strip()) < 120 and
                    not screenshot and not parsed.get('images') and not parsed.get('attachment_items')):
                canvas = await page.query_selector('main canvas, article canvas, .bbsCont canvas, .board_view canvas')
                if canvas is not None:
                    box = await canvas.bounding_box()
                    if (box and 200 <= box['width'] <= 2000 and 120 <= box['height'] <= 2000
                            and box['width'] * box['height'] <= 2_000_000):
                        screenshot = await canvas.screenshot(type='png', timeout=1500)
            return parsed, screenshot

        try:
            parsed, screenshot = await asyncio.wait_for(read_page(), timeout=budget)
            if parsed is None:
                return None
            if screenshot:
                from services.local_document_process import extract_document_isolated
                parsed['source_page'] = page_coverage.with_reasons(parsed.get('source_page'), 'rendered_capture_partial')
                try:
                    document = await asyncio.wait_for(extract_document_isolated(
                        screenshot, 'image', max_chars=15000, timeout=12), timeout=13)
                except Exception:
                    document = None
                if document is not None and document.text.strip():
                    parsed['text'] = '\n'.join(filter(None, [parsed.get('text', ''), document.text]))[:15000]
                    parsed['policy_text'] = parsed['text']
                    parsed['source_page'] = page_coverage.with_methods(
                        page_coverage.with_reasons(parsed.get('source_page'), 'rendered_capture_partial'), 'local_ocr')
                    if parsed.get('_captured_poster_url'):
                        parsed['local_capture_image_url'] = parsed['_captured_poster_url']
            parsed.pop('_captured_poster_url', None)
            if not (parsed.get('text') or parsed.get('images') or parsed.get('attachment_items')):
                return None
            return parsed
        except Exception as error:
            logger.info('공개 페이지 브라우저 보강을 건너뜁니다: %s', type(error).__name__)
            return None
        finally:
            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=2)
                except Exception:
                    pass

    async def fetch_rendered_text(self, url: str, max_chars: int = 15000, *,
                                  url_validator=None, policy_draft=False):
        """JS 렌더링 상세 페이지의 본문 텍스트를 Playwright로 추출 (Gemini 불필요).

        정적 aiohttp fetch가 빈 껍데기만 받는 SPA/iframe 구조(NHIS '?mode=view' 등)용
        저비용 fallback. Vision OCR(비쌈) 이전 단계로 시도한다. iframe 본문도 수집.
        """
        if not self.browser or not url:
            return ''
        context = await new_public_context(self.browser,
            ignore_https_errors=True,
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
            ),
        )
        try:
            # 사용자가 직접 넣은 URL을 렌더링할 때 페이지 리다이렉트·iframe·XHR가
            # 로컬/사설망을 읽지 못하게 모든 http(s) 요청의 목적지를 검사한다.
            # 동일 출처의 이미지·스크립트는 캐시해 DNS 검사를 한 번만 수행한다.
            if url_validator is not None:
                from urllib.parse import urlparse
                validation_cache = {}

                async def _guard_request(route, request):
                    request_url = request.url
                    parsed = urlparse(request_url)
                    if parsed.scheme not in ("http", "https"):
                        await route.fallback()
                        return
                    origin = (parsed.scheme, (parsed.hostname or "").lower(), parsed.port)
                    allowed = validation_cache.get(origin)
                    if allowed is None:
                        try:
                            await url_validator(request_url)
                            allowed = True
                        except Exception:
                            allowed = False
                        validation_cache[origin] = allowed
                    if allowed:
                        await route.fallback()
                    else:
                        logger.warning(f"렌더링 중 내부망/잘못된 요청 차단: {request_url[:120]}")
                        await route.abort("blockedbyclient")

                await context.route("**/*", _guard_request)
            page = await context.new_page()
            await page.add_init_script(_INIT_SCRIPT_PROTECT_GLOBALS)
            await page.goto(url, wait_until='domcontentloaded', timeout=_PW_VISION_TIMEOUT)
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(1)
            if policy_draft:
                from services.policy_draft_source import rendered_draft_source
                return await rendered_draft_source(page, url)
            text = (await page.inner_text('body')) or ''
            # iframe 내부 본문도 합침 (NHIS 등 프레임 구조 대응)
            for frame in page.frames:
                try:
                    if frame is page.main_frame:
                        continue
                    ftxt = await frame.inner_text('body')
                    if ftxt and ftxt.strip() not in text:
                        text = f"{text}\n{ftxt}"
                except Exception:
                    pass
            return text.strip()[:max_chars]
        except Exception as e:
            logger.warning(f"fetch_rendered_text failed for {url}: {e}")
            if policy_draft:
                return {'draft_source_failed': True}
            return ''
        finally:
            await context.close()

    async def extract_poster_url(self, url: str) -> str:
        """JS 렌더링 후 실제 본문 포스터 이미지 URL을 추출 (없으면 '').

        familynet 등은 포스터가 JS로 로드돼 정적 HTML에 없으므로 렌더가 필요하다.
        ai_classifier.select_poster_images(로고/헤더/푸터 제외)로 최적 포스터 1건 선정.
        """
        if not self.browser or not url:
            return ''
        import ai_classifier
        from bs4 import BeautifulSoup
        context = await new_public_context(self.browser,ignore_https_errors=True)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until='networkidle', timeout=_PW_VISION_TIMEOUT)
            await page.wait_for_timeout(1500)
            html = await page.content()
            posters = ai_classifier.select_poster_images(BeautifulSoup(html, 'html.parser'), url)
            return posters[0] if posters else ''
        except Exception as e:
            logger.warning(f"extract_poster_url failed for {url}: {e}")
            return ''
        finally:
            await context.close()

    async def scrape_site(self, site_config: Dict[str, Any], known_links: Optional[set] = None, *, with_status=False):
        """
        Scrapes a single site based on its configuration asynchronously.
        셀렉터가 없는 경우 _scrape_fallback()으로 링크 기반 추출.

        known_links: 이미 DB에 저장된 링크 집합(set). 전달하면 한 페이지의 모든
                     링크가 이미 알려진 경우 이후 페이지를 건너뛰는 조기 종료가 활성화됨.
        with_status: 새 수집 경로만 {state, items}를 반환한다. 목록/다음 페이지
                     오류와 정상 끝을 구분하며 기존 호출의 list 계약은 유지한다.
        """
        if not self.browser:
            await self.start()

        results: List[Dict[str, Any]] = []
        collection_failed = False
        review_status = {'vision_review_required': False}
        def outcome(items):
            if site_config.get('news_article_only'):
                items = _filter_article_links(items, site_config)
            cleaned = _clean_items(items)
            if not with_status:
                return cleaned
            return {'state': ('partial' if cleaned else 'failed') if collection_failed else 'finished',
                    'items': cleaned, 'vision_review_required': review_status['vision_review_required']}
        name = site_config['name']
        url = site_config['url']
        selectors = site_config.get('selectors') or {}
        base_url = site_config.get('base_url', '')
        max_pages = site_config.get('max_pages', 1)
        visited_list_pages, seen_list_pages = {url}, set()
        # Default to 1 page if not specified. User can increase this in config (DB/JSON).

        logger.info(f"Scraping {name} (Max Pages: {max_pages})...")

        # Enhanced Security Bypass Context
        context = await new_public_context(self.browser,
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.add_init_script(_INIT_SCRIPT_PROTECT_GLOBALS)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_PW_GOTO_TIMEOUT)

            # 셀렉터 없으면 fallback 모드
            if not selectors or not selectors.get('list_item'):
                if site_config.get('news_article_only'):
                    results, pagination_status = await self._scrape_news_fallback_pages(
                        page, site_config, strict=with_status, review_status=review_status)
                    collection_failed = pagination_status not in ('page_limit', 'end')
                else:
                    results = await self._scrape_fallback(page, site_config, strict=with_status,
                                                          review_status=review_status)
                return outcome(results)

            for current_page in range(1, max_pages + 1):
                logger.info(f"   -> Processing Page {current_page}...")

                # 1. Wait for list item (Essential)
                from playwright.async_api import TimeoutError as PlaywrightTimeout
                try:
                    await page.wait_for_selector(selectors['list_item'], timeout=_PW_SELECT_TIMEOUT)
                except PlaywrightTimeout:
                    logger.warning(f"Timeout waiting for selector on page {current_page} of {name}")
                    collection_failed = True
                    break # Stop pagination if list is gone
                except Exception as e:
                    logger.warning(f"Selector error in {name}: {e}")
                    collection_failed = True
                    break
                
                # 2. Extract Items via JavaScript for massively faster execution (No IPC overhead)
                page_results = await page.evaluate(r'''
                    ({listSel, titleSel, dateSel, siteName, baseUrl, programCards, knownBoard}) => {
                        const results = [];
                        const items = document.querySelectorAll(listSel);
                        
                        for (const item of items) {
                            try {
                                const titleEl = item.querySelector(titleSel);
                                const dateEl = dateSel ? item.querySelector(dateSel) : null;
                                
                                if (!titleEl) continue;
                                
                                let title = titleEl.innerText || titleEl.textContent;
                                title = title ? title.trim() : "";
                                if (knownBoard) {
                                    const titleCopy = titleEl.cloneNode(true);
                                    titleCopy.querySelectorAll('.bbs-ico').forEach(badge => badge.remove());
                                    title = (titleCopy.textContent || '').trim().replace(/\s+/g, ' ');
                                }
                                
                                const card = programCards ? titleEl.closest('.item') : null;
                                const cardTitle = card?.querySelector('.cont > .tit')?.textContent?.trim();
                                const linkEl = cardTitle ? (card.querySelector('.cont > a[href]') || titleEl) : titleEl;
                                if (cardTitle) title = cardTitle;
                                let link = linkEl.getAttribute('href') || "";
                                const rawHref = link;
                                const scriptLink = !link || link.startsWith('#') || /^\s*javascript:/i.test(link);
                                // Python resolves only known board calls. Unknown
                                // JavaScript stays non-clickable; never evaluate it.
                                if (scriptLink) link = "";

                                let date = "";
                                if (dateEl) {
                                    date = dateEl.innerText || dateEl.textContent;
                                    date = date ? date.trim() : "";
                                }
                                let publicationText = item.innerText || '';
                                const publishedTime = item.querySelector('[itemprop="datePublished"], time[pubdate]');
                                if (publishedTime) publicationText = '게시일: ' + (publishedTime.getAttribute('datetime') || publishedTime.getAttribute('content') || publishedTime.textContent || '');
                                if (dateEl) {
                                    const cell = dateEl.closest('td');
                                    const header = cell && cell.closest('table')?.querySelectorAll('thead th')[cell.cellIndex];
                                    const label = header?.textContent || dateEl.getAttribute('aria-label') || '';
                                    if (/게시일|등록일|작성일|발행일/.test(label) && !/마감|신청|접수|수정/.test(label))
                                        publicationText += '\n' + label + ': ' + date;
                                }
                                const navigation = titleEl.closest('nav, footer, [role="navigation"], #header, #footer, #gnb, #lnb, #snb, .gnb, .lnb, .snb, .breadcrumb, .breadcrumbs, .banner, .partners, .family-site, .family_site');
                                const header = titleEl.closest('header');
                                const pageHeader = header && !header.closest('article');
                                const articleContext = titleEl.closest('tr, article, [class*="board"], [id*="board"], [class*="bbs"], [id*="bbs"], [class*="notice"], [id*="notice"]') || !!dateEl;
                                
                                if (link && !link.startsWith('http')) {
                                    // 표준 URL 결합: 쿼리/경로/루트 상대링크를 현재 페이지 기준으로 정확히 해석.
                                    // (문자열 이어붙이기 시 '?mode=view'가 게시판 .do 경로를 잃던 버그 수정)
                                    try {
                                        link = new URL(link, window.location.href).href;
                                    } catch(e) { link = ""; }
                                }
                                
                                results.push({
                                    site_name: siteName,
                                    title: title,
                                    date: date,
                                    link: link,
                                    _board_page: window.location.href, _board_script: scriptLink,
                                    _board_href: rawHref, _board_onclick: linkEl.getAttribute('onclick') || '',
                                    _board_card: !!cardTitle && !scriptLink,
                                    _publication_text: publicationText,
                                    _in_navigation: !!(navigation || pageHeader),
                                    _article_context: !!articleContext
                                });
                            } catch (err) {
                                console.error(err);
                            }
                        }
                        return results;
                    }
                ''', {
                    'listSel': selectors['list_item'],
                    'titleSel': selectors['title'],
                    'dateSel': selectors.get('date', ''),
                    'siteName': name,
                    'baseUrl': base_url,
                    'programCards': board_kind(url) == 'program',
                    'knownBoard': bool(board_kind(url)),
                })
                page_results = materialize_board_links(page_results, url)
                
                if site_config.get('news_article_only'):
                    page_results = _filter_article_links(page_results, site_config)
                count = len(page_results)
                if site_config.get('news_article_only'):
                    identity = tuple(sorted(item.get('link', '') for item in page_results))
                    if current_page > 1 and (not identity or identity in seen_list_pages):
                        collection_failed = True
                        break
                    seen_list_pages.add(identity)
                logger.info(f"      Found {count} items on page {current_page}")
                results.extend(page_results)
                if not count:
                    # A broad selector matching layout rows is not a valid board;
                    # do not repeat pagination before the no-cost link fallback.
                    break

                # 조기 종료: 이 페이지의 모든 링크가 이미 DB에 있으면 다음 페이지는 불필요
                if known_links is not None and count > 0:
                    page_links = [item['link'] for item in page_results if item.get('link')]
                    if page_links and all(link in known_links for link in page_links):
                        logger.info(
                            f"   -> {name}: 페이지 {current_page}의 항목 {count}건 모두 기존 수집 데이터"
                            f" — 이후 페이지 건너뜀 (조기 종료)"
                        )
                        break

                # 3. Pagination Logic (If we need to go to next page)
                if current_page < max_pages:
                    if site_config.get('news_article_only'):
                        status = await self._advance_news_page(page, site_config, visited_list_pages)
                        if status != 'next':
                            collection_failed = status != 'end'
                            break
                        continue
                    # Try to find "Next" button
                    next_selector = site_config.get('next_page_selector')
                    
                    # Common Heuristics if not provided
                    common_next_selectors = [
                        'a:has-text("다음")', 'a:has-text("Next")', 'a:has-text(">")', 'a.next', 'a.btn-next', 
                        'button:has-text("다음")', 'button:has-text("Next")', '.pagination .next', '.paging .next'
                    ]
                    
                    next_btn = None
                    if next_selector:
                        next_btn = await page.query_selector(next_selector)
                    
                    if not next_btn:
                        for sel in common_next_selectors:
                            try:
                                next_btn = await page.query_selector(sel)
                                if next_btn:
                                    # Check visibility
                                    if await next_btn.is_visible():
                                        logger.info(f"      Found Next button via heuristic: {sel}")
                                        break
                                    else:
                                        next_btn = None # Reset if hidden
                            except Exception:
                                continue
                    
                    if next_btn:
                        try:
                            # Verify if disabled?
                            # Some sites use 'disabled' class or attribute
                            class_attr = await next_btn.get_attribute('class') or ""
                            if "disabled" in class_attr.lower():
                                logger.info("      Next button is disabled. End of list.")
                                break
                                
                            await next_btn.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=_PW_GOTO_TIMEOUT)
                            # Short sleep for SPA updates?
                            await asyncio.sleep(2)
                        except Exception as e:
                            logger.warning(f"      Failed to click next button: {e}")
                            collection_failed = True
                            break
                    else:
                        logger.info("      No 'Next' button found. Stopping pagination.")
                        break

            # 셀렉터가 있었지만 결과가 없으면 링크 기반 fallback 시도
            if not results:
                logger.warning(f"'{selectors.get('list_item')}' 셀렉터로 결과 없음 ({name}). 링크 fallback 시도...")
                try:
                    # If page 1 was already loaded, do not reload the same page
                    # and repeat its network/JS work after a selector timeout.
                    if current_page > 1:
                        await page.goto(url, wait_until="domcontentloaded", timeout=_PW_GOTO_TIMEOUT)
                    if site_config.get('news_article_only'):
                        fallback_items, pagination_status = await self._scrape_news_fallback_pages(
                            page, site_config, strict=with_status, review_status=review_status)
                        collection_failed = collection_failed or pagination_status not in ('page_limit', 'end')
                    else:
                        fallback_items = await self._scrape_fallback(page, site_config, strict=with_status,
                                                                     review_status=review_status)
                    if fallback_items:
                        logger.info(f"   -> 링크 fallback 성공: {len(fallback_items)}개 항목")
                        results = fallback_items
                except Exception as fb_e:
                    logger.warning(f"   -> 링크 fallback 실패 ({name}): {fb_e}")
                    collection_failed = True

        except Exception as e:
            logger.error(f"Failed to scrape {name}: {e}")
            collection_failed = True
        finally:
            try:
                await asyncio.wait_for(page.close(), timeout=2.5)
            except Exception:
                pass
            try:
                await asyncio.wait_for(context.close(), timeout=2.5)
            except Exception:
                pass

        return outcome(results)

    async def close(self) -> None:
        """
        Closes the browser and playwright instance.
        """
        if self.browser:
            try:
                await asyncio.wait_for(self.browser.close(), timeout=2.5)
            except Exception:
                pass
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=2.5)
            except Exception:
                pass

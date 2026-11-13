from google import genai
from google.genai import types
import os
import re
import json
import random
import aiohttp
from services.public_transport import public_get
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional
import asyncio

# Load environment variables
import utils
from services.logger import setup_logger
from services.ai_generation_safety import (
    AiResponseReviewRequired, STRUCTURED_OUTPUT_TOKENS, complete_response_text,
)

utils.load_environment()

logger = setup_logger(__name__)

# 환경변수 기반 타임아웃 (ai_classifier.py 내 HTTP 요청 타임아웃)
_AI_FETCH_TIMEOUT = int(os.getenv('AI_FETCH_TIMEOUT_SEC', '10'))

# 포스터 선별: 로고/아이콘/버튼/배너 등 UI 이미지를 걸러내고 실제 본문 포스터만 남긴다.
# (기존엔 페이지의 첫 <img> 2장을 OCR해 거의 항상 로고를 분석하던 문제를 해결)
_IMG_ICON_RE = re.compile(
    r'(icon|logo|btn|button|bullet|blank|spacer|common|header|footer|gnb|lnb|menu|'
    r'sns|symbol|thumb|arrow|sprite|favicon|loading|print|share|facebook|insta|'
    r'instagram|youtube|blog|kakao|twitter|naver|close|fill|prev|next|zoom|search|'
    r'qrcode|/qr/|qr_|_qr|/img/main/|/images/header|/images/sub/|/images/\d+/main/|'
    r'/skin|/theme/|/basic/img|/img/common|/home\.|home\.png|home\.gif)', re.I)
# 본문 포스터일 가능성↑ 경로 (프로그램/에디터/첨부 등). /board·/upload/board는 로고도 섞여 낮게.
_IMG_GOOD_PATH_RE = re.compile(
    r'(/upload/program|/upload/editor|/editor|/se2|/cheditor|/userfile|/attach|'
    r'/filedata|/imageview|__icsfiles|/afieldfile|/data/editor|/upload_program|'
    r'thumbnails\.do)', re.I)
# 헤더/푸터/네비/로고 컨테이너 (조상 클래스·id) → 그 안 이미지는 UI(로고/아이콘)
_BAD_ANCESTOR_RE = re.compile(
    r'(header|footer|gnb|lnb|snb|nav|util|sns|logo|menu|banner|breadcrumb|top_|'
    r'_top|h_wrap|f_wrap|allmenu|side_menu)', re.I)
# UI성 alt 텍스트
_BAD_ALT_RE = re.compile(r'(로고|logo|메뉴|바로가기|아이콘|버튼|배너|홈으로|닫기|열기|이전|다음|가족센터)', re.I)


def _resolve_thumbnail(u: str, base_url: str) -> str:
    """thumbnails.do?filename=/upload/... 프록시 → 원본 풀사이즈 URL로 해석."""
    from urllib.parse import urlparse, parse_qs, urljoin
    if 'thumbnails.do' in u.lower() and 'filename=' in u:
        try:
            fn = parse_qs(urlparse(u).query).get('filename', [''])[0]
            if fn:
                pr = urlparse(base_url)
                return urljoin(f"{pr.scheme}://{pr.netloc}", fn)
        except Exception:
            pass
    return u


def _in_bad_container(img) -> bool:
    p = img.parent
    for _ in range(5):
        if p is None or not hasattr(p, 'get'):
            break
        cls = ' '.join(p.get('class') or []) if p.get('class') else ''
        pid = p.get('id') or ''
        if _BAD_ANCESTOR_RE.search(f"{cls} {pid}"):
            return True
        p = p.parent
    return False


def select_poster_images(soup, base_url: str) -> List[str]:
    """본문 포스터로 보이는 이미지 URL을 점수순으로 반환 (로고/아이콘/헤더·푸터 제외)."""
    from urllib.parse import urljoin
    def _dim(v):
        try:
            return int(re.sub(r'\D', '', str(v)) or 0)
        except (ValueError, TypeError):
            return 0
    cands = []
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-original') or ''
        if not src or src.startswith('data:'):
            continue
        u = _resolve_thumbnail(urljoin(base_url, src), base_url)
        low = u.lower()
        if low.endswith('.svg') or low.endswith('.gif'):
            continue
        if _IMG_ICON_RE.search(low):
            continue
        if _BAD_ALT_RE.search(img.get('alt') or ''):
            continue
        if _in_bad_container(img):          # 헤더/푸터/네비 안의 로고 제외
            continue
        w, h = _dim(img.get('width')), _dim(img.get('height'))
        if (w and w < 200) or (h and h < 200):
            continue
        score = (3 if _IMG_GOOD_PATH_RE.search(low) else 0) \
            + (1 if '/upload' in low else 0) \
            + (1 if low.rsplit('.', 1)[-1] in ('jpg', 'jpeg', 'png') else 0)
        if u not in [c[1] for c in cands]:
            cands.append((score, u))
    cands.sort(key=lambda x: -x[0])
    return [u for _, u in cands]

# Categories defined by user
CATEGORIES: List[str] = [
    "의료/재활", "교육/보육", "가족지원", "돌봄/양육", "복지", "생활지원"
]

# Target characteristics defined by user
TARGETS: List[str] = [
    "일반", "장애/발달지연", "저소득", "임산부", "보호자", "다문화", "다자녀", "한부모"
]

def get_client():
    """Returns a configured GenAI client. Reads API key fresh each time."""
    current_key = os.getenv("GOOGLE_API_KEY")
    if not current_key:
        return None
    from services import ai_coordinator
    return ai_coordinator.create_client(current_key)

def _parse_page_html(html: str, url: str) -> Dict[str, Any]:
    """BeautifulSoup 파싱(CPU 바운드 동기 작업)을 순수 함수로 분리.

    fetch_page_content_async가 asyncio.to_thread로 이 함수를 별도 스레드에서
    실행하므로, AI 분석 동시처리 중에도 이벤트 루프(/api/status 등)가 막히지 않는다."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, 'html.parser')
    # Recognize a complete bulletin layout, never a lone generic content div.
    # Keep the original tree for policy section selection and attachment discovery.
    bulletin = None
    bulletin_title = ''
    views = soup.select('div.bbsView')
    if len(views) == 1:
        view = views[0]
        metadata = view.select('table.boardView')
        bodies = view.select('div.bbsCont')
        titles = view.select('table.boardView td.title')
        if len(metadata) == len(bodies) == len(titles) == 1 and bodies[0].get_text(' ', strip=True):
            bulletin_title = titles[0].get_text(' ', strip=True)
            if bulletin_title:
                bulletin = BeautifulSoup(str(metadata[0]) + str(bodies[0]), 'html.parser')
                for unwanted in bulletin.select('script, style, nav, footer, header, caption, .bbsBtn'):
                    unwanted.decompose()

    # Extract images — 포스터 우선 선별 (로고/아이콘 제외, 본문 포스터 먼저)
    images = select_poster_images(soup, url)

    # 공공기관 게시판은 확장자가 없는 fileDown.do 주소와 링크 문구/Content-Disposition에만
    # 실제 파일명이 있는 경우가 많다. 공통 탐지기가 직렬화 가능한 메타데이터를 만들고,
    # attachments에는 이전 호출자 호환용 URL 목록을 유지한다.
    from services.news_attachments import discover_attachments
    attachment_items = discover_attachments(soup, url)
    attachments = [item['url'] for item in attachment_items]

    # Remove script and style elements
    for script in soup(["script", "style", "nav", "footer", "header"]):
        script.decompose()

    text = (bulletin if bulletin is not None else soup).get_text(separator=' ', strip=True)
    # URL 직접 초안 기능은 사용자가 제목을 따로 입력하지 않아도 정책명을 제안해야 한다.
    # og:title → 본문 h1 → <title> 순으로 사람이 보는 제목에 가까운 값을 고른다.
    title = bulletin_title
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if not title and og_title and og_title.get("content"):
        title = str(og_title.get("content"))
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = re.sub(r"\s+", " ", title).strip()[:300]
    draft_title = title
    from urllib.parse import urlparse
    draft_host = (urlparse(url).hostname or '').lower()
    if draft_host == 'www.mohw.go.kr':
        headings = soup.select('#contents_title')
        if len(headings) == 1 and headings[0].get_text(strip=True):
            draft_title = headings[0].get_text(' ', strip=True)
    elif draft_host == 'www.doccic.go.kr':
        roots = soup.select('#content')
        if len(roots) == 1:
            heading = roots[0].find('h3')
            if heading and heading.get_text(strip=True):
                draft_title = heading.get_text(' ', strip=True)
    # Keep a separate structured representation for policy section selection.
    # Existing news text, attachment discovery and title extraction stay unchanged.
    # A policy catalogue can use sibling list items instead of h-tags.
    # Mark only outer catalogue items; nested eligibility bullets stay in scope.
    for listing in soup.find_all(['ul', 'ol']):
        if listing.find_parent('li'):
            continue
        items = listing.find_all('li', recursive=False)
        if not any(item.find(['ul', 'ol'], recursive=False) for item in items):
            continue
        for item in items:
            leading = next((str(child).strip().splitlines()[0].strip()
                            for child in item.children if isinstance(child, str) and str(child).strip()), '')
            if leading and len(leading) <= 200:
                item.insert_before(soup.new_string('\n###### ' + leading + '\n'))
    for heading in soup.find_all(re.compile(r'^h[1-6]$')):
        level = int(heading.name[1])
        heading.replace_with('\n' + '#' * level + ' ' + heading.get_text(' ', strip=True) + '\n')
    policy_text = soup.get_text(separator='\n', strip=True)
    # URL drafts can use an explicit, single HTML main/article boundary.
    # Never infer a boundary from a keyword or crop a long policy. Keep the
    # existing representations unchanged for collection/update callers.
    from services.policy_draft_source import draft_body
    draft_text = draft_body(soup, url).get_text(separator='\n', strip=True)
    from services import page_coverage
    coverage = page_coverage.make(*(['page_char_limit'] if len(text) > 15000 else
                                  (['no_text'] if not text else [])))
    return {"text": text[:15000], "source_page": coverage, "images": images, "attachments": attachments,
            "attachment_items": attachment_items,
            "title": title, "policy_text": policy_text[:15000],
            "draft_text": draft_text[:15000], "draft_title": draft_title[:300]}


async def fetch_page_content_async(session: aiohttp.ClientSession, url: str, *,
                                   url_validator=None) -> Dict[str, Any]:
    """
    Fetches the main text content and images from a URL asynchronously.
    Returns: {"text": str, "images": List[str]}
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        request_url = url
        response_context = None
        # URL 직접 초안처럼 사용자가 임의 주소를 넣는 경로는 리다이렉트 목적지도
        # 매번 검사한다. 최초 주소만 확인한 뒤 자동 리다이렉트를 허용하면 공개 주소가
        # 내부망 주소로 넘겨주는 방식으로 SSRF 방어를 우회할 수 있다.
        for _redirect_count in range(6):
            if url_validator is not None:
                # DNS-backed safety validation happens before aiohttp's own
                # timeout begins. Bound it too, without skipping the check or
                # allowing redirects to bypass validation of their destination.
                request_url = await asyncio.wait_for(
                    url_validator(request_url), timeout=_AI_FETCH_TIMEOUT)
            response_context = public_get(session,
                request_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_AI_FETCH_TIMEOUT),
                allow_redirects=url_validator is None,
            )
            response = await response_context.__aenter__()
            if url_validator is not None and response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                await response_context.__aexit__(None, None, None)
                response_context = None
                if not location:
                    raise ValueError("리다이렉트 주소가 비어 있습니다.")
                from urllib.parse import urljoin
                request_url = urljoin(request_url, location)
                continue
            break
        else:
            raise ValueError("리다이렉트가 너무 많습니다.")

        try:
            response.raise_for_status()
            try:
                max_bytes = int(os.getenv('AI_FETCH_MAX_BYTES', '2000000'))
            except (TypeError, ValueError):
                max_bytes = 2000000
            max_bytes = max(65536, min(max_bytes, 10000000))
            # StreamReader.read(n) may return a partial network chunk before EOF.
            # Read until EOF (or the existing cap), otherwise footer-side attachments
            # intermittently disappear depending on packet timing.
            chunks = []
            received = 0
            while received <= max_bytes:
                chunk = await response.content.read(min(65536, max_bytes + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            raw = b''.join(chunks)
            byte_limited = len(raw) > max_bytes
            if byte_limited:
                logger.warning(f"본문 응답이 {max_bytes}바이트를 초과해 안전하게 잘랐습니다: {url}")
                raw = raw[:max_bytes]
            decode_failed = False
            try:
                html = raw.decode(response.charset or 'utf-8', errors='strict')
            except UnicodeDecodeError:
                decode_failed = True
                html = raw.decode(response.charset or 'utf-8', errors='replace')
            parsed = await asyncio.to_thread(_parse_page_html, html, request_url)
            from services import page_coverage
            parsed['source_page'] = page_coverage.with_reasons(parsed.get('source_page'),
                *(['html_byte_limit'] if byte_limited else []), *(['decode_error'] if decode_failed else []))
            parsed["content_type"] = str(response.headers.get("Content-Type") or "")
            parsed["final_url"] = str(response.url)
            return parsed
        finally:
            if response_context is not None:
                await response_context.__aexit__(None, None, None)

    except Exception as e:
        from services import page_coverage
        failure_kind = 'network'
        if isinstance(e, aiohttp.ClientResponseError):
            failure_kind = {401: 'unauthorized', 404: 'not_found', 410: 'not_found',
                            429: 'rate_limited'}.get(e.status, 'http_error')
        elif isinstance(e, ValueError):
            failure_kind = 'unsafe_url'
        elif isinstance(e, TimeoutError):
            failure_kind = 'timeout'
        logger.warning(f"Error fetching content from {url} [{failure_kind}]: {e}")
        return {"text": "", "images": [], "attachments": [], "attachment_items": [], "title": "",
                "content_type": "", "final_url": url, "source_page": page_coverage.make('fetch_failed'),
                "fetch_failure_kind": failure_kind}

async def _call_gemini_json(client: genai.Client, prompt: str, title: str, *, model_override=None, response_json_schema=None, request_attempts=3, verified_input=None, before_dispatch=None) -> Dict[str, Any]:
    """Gemini 호출 공통 로직: JSON 파싱 + 429/503 재시도. 실패 시 예외를 던진다(호출자가 폴백 결정).

    temperature=0으로 변동을 줄인다. 같은 입력에서도 응답 차이가 발생할 수 있으므로
    동일 결과를 보장하는 옵션으로 취급하지 않는다.
    """
    # Durable workers own their retry schedule; one claim must not multiply
    # into three SDK requests. Direct callers retain the existing retry policy.
    if type(request_attempts) is not int or not 1 <= request_attempts <= 3:
        raise ValueError('Request attempts must be an integer from 1 to 3')
    from services.ai_input_safety import ensure_ai_text_safe
    ensure_ai_text_safe(prompt)
    _MAX_RETRIES = request_attempts
    model_name = model_override if model_override is not None else os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
    from services import ai_coordinator
    if verified_input is not None:
        from services.ai_verified_input import input_tokens_for
        request_input_tokens = input_tokens_for(verified_input, model=model_name,
            prompt=prompt, response_json_schema=response_json_schema)
    else:
        # Existing recipes retain their original conservative accounting.
        request_input_tokens = ai_coordinator.estimate_generation_input_tokens(prompt, response_json_schema)
    for _attempt in range(_MAX_RETRIES):
        _guard_pending = False
        try:
            from services import ai_coordinator
            await ai_coordinator.acquire_model_slot(model_name, input_tokens=request_input_tokens)
            # Quota pacing may have waited while this durable job was cancelled.
            # Keep the conservative reservation, but never send a stale claim.
            if before_dispatch is not None:
                _guard_pending = True
                await before_dispatch()
                _guard_pending = False
            response = await ai_coordinator.await_api(client.aio.models.generate_content(
                # 기본 모델은 flash-lite이며 CLASSIFY_MODEL로 지정할 수 있다.
                # 무료 한도는 프로젝트·모델·등급에 따라 달라 AI Studio의 실제 값을
                # 확인해야 한다. 모델 이름이나 같은 lite 등급이 동일 품질을 보장하지 않는다.
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_json_schema=response_json_schema,
                    temperature=0,
                    max_output_tokens=STRUCTURED_OUTPUT_TOKENS,
                    candidate_count=1,
                )
            ))
            text = complete_response_text(response)
            try:
                return json.loads(text)
            except Exception:
                # 슬라이싱(text[7:-3])은 펜스 표기가 "```json"에서 한 글자라도 어긋나면
                # (예: "``` json", "```JSON") 엉뚱한 위치를 잘라 JSONDecodeError를 낸다 —
                # 정규식으로 여는/닫는 펜스를 안전하게 제거.
                text = re.sub(r'^```\s*(?:json)?\s*', '', text, flags=re.IGNORECASE)
                text = re.sub(r'\s*```$', '', text)
                return json.loads(text)

        except Exception as _e:
            if _guard_pending:
                # Local ownership/cancellation errors are not provider errors
                # and must never trigger a provider retry or quota penalty.
                raise
            _err_str = str(_e)
            _error_info = ai_coordinator.register_api_error(model_name, _e)
            if _error_info.get('local_budget_exhausted'):
                logger.info('AI 요청을 보내지 않았습니다. 저장된 자료는 유지됩니다.')
                raise
            if _error_info['daily_exhausted']:
                logger.error(
                    "🚫 [일일 한도 소진] Google에서 분류 모델의 일일 요청 한도 초과를 알렸습니다. "
                    "자정(태평양 시간) 이후 자동 갱신됩니다."
                )
                raise
            if _error_info['rate_limited'] and _attempt < _MAX_RETRIES - 1:
                # (버그 2026-07-27, GPT Codex 리뷰) 병렬 처리 중인 여러 항목이 거의 같은
                # 순간에 함께 429를 맞으면 Gemini가 알려주는 retryDelay도 서로 비슷해, 다들
                # 똑같이 대기했다가 한꺼번에 재시도해 다시 429가 나는 "동시 재시도" 패턴이
                # 실측 로그에서 확인됐다(같은 초에 여러 항목이 "33초 후 재시도"). 무작위
                # 지터를 더해 재시도 시점을 흩어놓는다.
                _wait = (_error_info['retry_after'] + 2 if _error_info['retry_after'] else 30) + random.uniform(0, 5)
                ai_coordinator.set_model_cooldown(model_name, _wait)
                logger.warning(
                    f"⏳ [429] Gemini 분당 한도 감지 — {_wait:.0f}초 후 재시도... "
                    f"(시도 {_attempt + 1}/{_MAX_RETRIES}) | {title[:30]}"
                )
                await asyncio.sleep(_wait)
                continue
            if _error_info['unavailable'] and _attempt < _MAX_RETRIES - 1:
                _wait = 15
                logger.warning(
                    f"⏳ [503] Gemini 과부하 감지 — {_wait}초 후 재시도... "
                    f"(시도 {_attempt + 1}/{_MAX_RETRIES}) | {title[:30]}"
                )
                await asyncio.sleep(_wait)
                continue
            raise


async def classify_post_async(client: genai.Client, title: str, link: str, site_name: str = "", content: Optional[str] = None,
                              *, custom_prompt: Optional[str] = None, strict=False,
                              model_override=None, content_max_chars=None, input_strategy='balanced',
                              source_page=None, evidence_rules=True, request_attempts=3) -> Dict[str, Any]:
    """
    Classifies a post using Google Gemini API asynchronously.
    """
    if not client:
        if strict:
            raise ValueError('AI client is required')
        from services.legacy_news_analysis import failed_result
        return failed_result(reason='authentication_required', attempts=0)

    from services import news_ai_input, news_contact_input
    original_content = '' if content is None else content
    content, contact_metadata = news_contact_input.prepare_content(original_content)
    minimized_source = content
    content, input_coverage = news_ai_input.prepare(content, content_max_chars, strategy=input_strategy)
    input_coverage = news_contact_input.add_coverage_metadata(input_coverage, contact_metadata)
    if not content:
        content = "Content fetch skipped or failed. Analyze based on title only."

    try:
        # None preserves legacy callers; explicit empty string means no extra
        # instructions, not "read whatever the user changed during this run".
        if custom_prompt is None:
            custom_prompt = utils.get_env_value("CUSTOM_AI_PROMPT")
        from services.ai_input_safety import ensure_ai_text_safe
        ensure_ai_text_safe(title, link, site_name, custom_prompt)
        custom_instructions = ""
        if custom_prompt:
            custom_instructions = f"\n        [User Custom Instructions (CRITICAL)]\n        {custom_prompt}\n"

        prompt = f"""
        Analyze the following welfare post content and extract detailed information.
        
        [Post Info]
        Site: {site_name}
        Title: {title}
        Link: {link}
        Content: {content}
        
        [Categories]
        {', '.join(CATEGORIES)}
        
        [Relevance Criteria]
        The user is interested in:
        1. Welfare benefits (cash, voucher, service).
        2. Support programs (business support, education, job training, housing).
        3. Project/Program announcements (new recuitment, application open).
        
        The user is NOT interested in:
        1. Administrative notices (server maintenance, holiday hours, system check).
        2. Internal news (personnel appointments, awards, board meetings, employees only).
        3. Bid/Contract/Tender results (procurement, construction bids).
        4. Recruitment/Job Openings (staff hiring, unless it's a job training program for citizens).
        5. Winner Announcements (event winners, selection results - unless it explains the program itself).
        6. Simple news reports without actionable info.

        [Negative Constraints]
        - If the post is about a "Winner Announcement" (당첨자 발표, 선정 결과), set "is_relevant": false.
        - If the post is about "Recruiting Staff" (직원 채용), set "is_relevant": false.
        - If the post is a "Bid Notice" (입찰 공고), set "is_relevant": false.
        - If the content is a login/authentication page (로그인 폼, 회원 인증 화면, 아이디/비밀번호 입력), set "is_relevant": false.
        {custom_instructions}
        Return ONLY a JSON object with this format (Make sure to apply the custom instructions if any):
        {{
            "is_relevant": true/false,  
            "category": "One of: 의료/재활, 교육/보육, 가족지원, 돌봄/양육, 복지, 생활지원, 기타",
            "target": "Primary Target Group (e.g., 영유아)",
            "target_characteristics": ["Selected Tags from: 일반, 장애/발달지연, 저소득, 임산부, 보호자, 다문화, 다자녀, 한부모"],
            "start_age_months": integer (0 if birth) or null,
            "end_age_months": integer or null,
            "age_detail": "Detailed age text (e.g. 생후 14일~71개월)",
            "target_income": "Income text (e.g. 소득 무관, 중위소득 180% 이하)",
            "eligibility_detail": "Other eligibility requirements",
            "residence": ["Region tags e.g. 전국, 서울시, 도봉구"],
            "life_cycle": ["Tags from: 임신/출산, 영아, 유아, 학령기, 청소년, 청년, 중장년, 노년, 전생애"],
            "support_detail": "Notion-ready markdown. Use ### headings and bullet lists. Include only sections that have actual content:\\n### ✅ 지원 내용\\n- ...\\n\\n### 📋 신청 자격\\n- 대상자 및 자격 요건\\n\\n### 📍 신청 방법\\n- 방법 및 장소 (예: 주민센터 방문, 온라인 신청)\\n\\n### 💰 지원 금액 및 혜택\\n- 금액, 바우처, 서비스 내용\\n\\n### 📅 신청 기간\\n- 시작일 ~ 마감일\\n\\n### 📝 참고 사항\\n- 기타 안내",
            "contact": "Phone numbers",
            "deadline": "YYYY-MM-DD" or null,
            "event_date": "YYYY-MM-DD (행사/접수 시작일)" or null,
            "event_end_date": "YYYY-MM-DD (행사/접수 종료일, 시작일과 다를 때만)" or null,
            "page_modified_date": "YYYY-MM-DD (페이지의 최종수정일/최종업데이트일 표기 값, 없으면 null)" or null,
            "related_urls": ["List of relevant URLs"],
            "source_site_name": "Name of the original source site if mentioned (e.g. 보건복지부)",
            "source_site_url": "URL of the original source site if mentioned",
            "item_type": "program" or "event",
            "reasoning": "Brief explanation of why this category/target was chosen based on the text.",
            "evidence_snippet": "Exact quote from the text that supports the target/eligibility."
        }}

        [item_type 판단 기준]
        - "program": 자격 요건·소득 기준·신청 절차가 있는 복지 지원 사업 (지원금, 바우처, 서비스 제공 등)
        - "event": 예약/신청 오픈 공지, 단기 강좌·특강, 1회성 행사, 프로그램 참가 모집 등 날짜/기한이 핵심인 소식
        """
        
        if input_strategy == 'balanced':
            prompt += news_ai_input.prompt_notice(input_coverage)
            if evidence_rules:
                prompt += news_ai_input.FACT_RULES
        if contact_metadata['status'] == 'minimized':
            prompt += news_contact_input.PROMPT_NOTICE
        from services import page_coverage
        page_state = page_coverage.normalize(source_page)
        if page_state.get('status') == 'review_required':
            prompt += '\n[Source coverage] Page fetching or text extraction is incomplete. Missing text is not evidence of no restrictions. Leave unsupported facts empty/null.\n'
        from services import ai_coordinator
        request_budget = ai_coordinator.input_token_budget(
            model_override if model_override is not None else os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite'))
        if ai_coordinator.estimate_generation_input_tokens(prompt) > request_budget:
            from services.news_request_plan import build_news_request
            prompt, input_coverage = build_news_request(
                title, link, site_name, minimized_source, custom_prompt,
                input_token_budget=request_budget, content_max_chars=content_max_chars,
                input_strategy=input_strategy, source_page=page_state,
                evidence_rules=evidence_rules, contact_metadata=contact_metadata)
            input_coverage = news_contact_input.add_coverage_metadata(input_coverage, contact_metadata)
        if model_override is not None or request_attempts != 3:
            result = await _call_gemini_json(client, prompt, title, model_override=model_override,
                                           request_attempts=request_attempts)
        else:
            result = await _call_gemini_json(client, prompt, title)
        # Enrichment must not turn empty/malformed JSON into completed analysis.
        # Keep the same basic contract as the durable worker; dates are optional.
        from services.news_classification_result import validate_result
        try:
            validate_result(result)
        except (ValueError, TypeError):
            raise AiResponseReviewRequired('incomplete') from None
        # A provider response cannot define the local execution outcome.
        result.pop('analysis_status', None)
        result.pop('analysis_failure', None)
        news_contact_input.clear_model_contact(result, contact_metadata)
        if (input_strategy == 'balanced' or contact_metadata['status'] == 'minimized'
                or input_coverage.get('budget_limited')):
            result['source_input'] = input_coverage  # code-owned, never trust a model-supplied value
        result.pop('source_page', None)
        if page_state:
            result['source_page'] = page_state
        return result

    except Exception as e:
        if strict:
            raise
        logger.error(f"AI Analysis failed for '{title}': {e}")
        from services.legacy_news_analysis import failed_result
        return failed_result(e)


def policy_source_preflight_review(content):
    """Same source-only hold before task rate waiting and before extraction.

    No client, network, quota reservation, source mutation or model inference.
    Privacy and model-response checks remain in the extraction path.
    """
    if not isinstance(content, str) or not content.strip():
        return dict(code='source_missing', note='정책 원문이 없어 분석을 보류했습니다. 기존 내용은 유지됩니다.', fields=[])
    from services.policy_source_plan import policy_source_max_chars
    max_chars = policy_source_max_chars()
    if max_chars <= 0 or len(content) > max_chars:
        return dict(code='source_too_long', note='정책 원문이 분석 길이 한도를 넘어 보류했습니다. '
                    '해당 정책의 상세 출처로 연결한 뒤 다시 점검해주세요. 기존 내용은 유지됩니다.', fields=[])
    from services.policy_source_review import source_criteria_issues, source_criteria_note
    issues = source_criteria_issues(content)
    if issues:
        return dict(code='source_criteria_review_required', note=source_criteria_note(issues),
                    fields=list(dict.fromkeys(item['field'] for item in issues)))
    return {}


async def extract_policy_fields_async(client: genai.Client, title: str, link: str,
                                      site_name: str = "", content: Optional[str] = None,
                                      taxonomy: Optional[Dict[str, List[str]]] = None, *,
                                      review: Optional[Dict[str, Any]] = None,
                                      draft_preview: bool = False, before_send=None) -> Optional[Dict[str, Any]]:
    """정책 최신화(Stage 2) 전용 중립 추출. classify_post_async와 달리:
    - 수집 관점의 관련성 필터(is_relevant/Negative Constraints)를 넣지 않는다 —
      비교 목적은 "이 내용이 우리 관심사인가"가 아니라 "필드값이 무엇인가"이므로,
      수집 필터 언어가 섞이면 성인 대상이 포함된 정책 등에서 결과가 왜곡될 수 있다.
    - Settings의 CUSTOM_AI_PROMPT(영유아 중심 수집 지침)를 주입하지 않는다 — 그 지침은
      "무엇을 수집할지"를 위한 것이지 "이미 정해진 정책의 내용을 얼마나 충실히 옮기는지"와
      무관하며, 섞이면 추출 일관성이 떨어진다.
    실패 시 None 반환(호출자가 재시도/스킵 판단 — 수집용 폴백 더미값과 섞이지 않도록 구분).
    """
    if isinstance(review, dict):
        review.clear()

    def hold(code, note, fields=(), *, diagnostics=None, retryable=False):
        if isinstance(review, dict):
            review.update(code=code, note=note, fields=list(fields))
            if diagnostics is not None:
                from services.policy_evidence import public_policy_diagnostics
                review['diagnostics'] = public_policy_diagnostics(diagnostics)
            if retryable:
                review['retryable'] = True
        logger.warning(note)
        return None

    if not client:
        return None
    source_review = await asyncio.to_thread(policy_source_preflight_review, content)
    # A preview is not an update approval. Quote different income thresholds
    # together for human review; never silently choose one. Require a review
    # sink so no caller can accidentally discard the warning.
    income_review = (draft_preview is True and isinstance(review, dict)
                     and source_review.get('code') == 'source_criteria_review_required'
                     and source_review.get('fields') == ['target_income'])
    if source_review and not income_review:
        return hold(source_review['code'], source_review['note'], source_review['fields'])

    # 연결된 Notion DB의 실제 select/multi_select 선택지를 정책 추출 기준으로 사용한다.
    # 옵션명은 사용자가 관리하는 외부 입력이므로 프롬프트 비대화·제어문자 삽입을 막아
    # 짧은 JSON 문자열 값으로만 전달한다. 스키마가 없으면 기존 기본값을 그대로 유지한다.
    defaults = {
        "category": list(CATEGORIES),
        "target_characteristics": ["일반", "장애/발달지연", "저소득", "임산부", "보호자", "다문화", "다자녀", "한부모"],
        "residence": ["전국", "서울시", "도봉구"],
        "life_cycle": ["임신/출산", "영아", "유아", "학령기", "청소년", "청년", "중장년", "노년", "전생애"],
    }

    def safe_options(key: str) -> List[str]:
        raw = (taxonomy or {}).get(key)
        if not isinstance(raw, list):
            return defaults[key]
        result = []
        for value in raw[:100]:
            if not isinstance(value, str):
                continue
            value = re.sub(r'[\x00-\x1f\x7f]+', ' ', value).strip()[:80]
            if value and value not in result:
                result.append(value)
        # An explicitly empty option list is a valid Notion schema, not
        # permission to invent default tags absent from that policy table.
        return result

    allowed = {key: safe_options(key) for key in defaults}
    from services.policy_source_plan import (
        build_policy_extraction_request, hydrate_policy_source_ranges, PolicySourcePlanError,
        policy_response_matches_schema,
    )
    try:
        from services import ai_coordinator
        model_name = os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
        request_budget = ai_coordinator.input_token_capacity(model_name)
        if request_budget <= 0:
            return hold('source_input_count_unavailable',
                        'AI 입력 크기와 사용 한도를 확인하지 못해 전송을 보류했습니다. '
                        '연결·사용 한도를 확인한 뒤 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.',
                        retryable=True)
        prompt, response_schema = await asyncio.to_thread(build_policy_extraction_request,
            content, title, link, site_name, allowed, compact=draft_preview is True,
            input_token_budget=request_budget, defer_token_budget_check=True)
    except PolicySourcePlanError as error:
        if error.code == 'source_input_budget_exceeded':
            return hold('source_input_budget_exceeded',
                        '정책 원문과 필수 분석 지침이 현재 AI 입력 한도를 넘어 보류했습니다. '
                        '입력 한도와 상세 출처를 확인해주세요. 원문과 기존 내용은 유지됩니다.')
        return hold('source_too_long', '정책 원문 구조 또는 요청 크기가 분석 한도를 넘어 보류했습니다. '
                    '상세 출처를 확인해주세요. 원문을 자르거나 추가 요청하지 않았으며 기존 내용은 유지됩니다.')
    from services.ai_input_safety import AiInputReviewRequired, REVIEW_MESSAGE
    try:
        # countTokens is an external transmission too. Check the ENTIRE source
        # before any token request; the verifier checks the exact prompt/schema.
        from services.ai_input_safety import ensure_ai_text_safe
        from services.ai_verified_input import verify_input
        ensure_ai_text_safe(content)
        verified = await verify_input(client, prompt, response_schema,
            model=model_name, input_token_budget=request_budget)
        if verified.input_tokens > request_budget:
            if not verified.is_provider_counted:
                return hold('source_input_count_unavailable',
                            'AI 입력 크기를 확인하지 못해 분석을 잠시 보류했습니다. '
                            '연결 상태를 확인하고 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.',
                            retryable=True)
            return hold('source_input_budget_exceeded',
                        '정책 원문과 필수 분석 지침이 현재 AI 입력 한도를 넘어 보류했습니다. '
                        '입력 한도와 상세 출처를 확인해주세요. 원문과 기존 내용은 유지됩니다.')
        if before_send is not None:
            # The optional workflow limiter counts prepared transmission attempts,
            # not inputs already rejected by source/size/privacy preflight. The
            # shared coordinator still rechecks and reserves every SDK attempt.
            # Do not refund this callback after provider or response failures.
            from services.ai_input_safety import ensure_ai_text_safe
            ensure_ai_text_safe(prompt)
            await before_send()
        result = await _call_gemini_json(client, prompt, title, response_json_schema=response_schema,
                                       model_override=model_name, verified_input=verified)
        if result is None:
            return hold('model_abstained',
                        'AI가 정책 내용을 확정해 추출하지 못했습니다. 원문의 조건과 예외를 직접 확인해주세요. '
                        '기존 내용은 유지됩니다.')
        raw_selection = result if isinstance(result, dict) and '_source_units' in result else None
        try:
            result = await asyncio.to_thread(hydrate_policy_source_ranges, result, content)
        except PolicySourcePlanError as error:
            if error.code in {'source_sections_incomplete', 'source_range_context_changed'}:
                return hold('source_conditions_incomplete',
                            '원문의 지원 내용·신청 방법·제외 조건·제출서류 구간이나 앞뒤 문맥이 충분히 선택되지 않아 '
                            '보류했습니다. 원문 전체를 확인해주세요. 기존 내용은 유지됩니다.',
                            ('support_detail', 'eligibility_detail', 'age_detail', 'target_income'))
            return hold('source_range_invalid',
                        'AI가 선택한 원문 구간의 형식을 확인하지 못해 보류했습니다. '
                        '추가 요청하지 않았으며 기존 내용은 유지됩니다.')
        from services.policy_evidence import diagnose_policy_evidence
        diagnostic = await asyncio.to_thread(diagnose_policy_evidence, result, content,
                                            draft_income_review=income_review)
        if not diagnostic['valid']:
            reasons = {item['code'] for item in diagnostic['issues']}
            fields = diagnostic['fields']
            if 'structured_fact_unsupported' in reasons:
                return hold('structured_evidence_missing',
                            '정책의 지역·대상·나이·연락처 근거를 확인하지 못해 보류했습니다. '
                            '원문을 확인해주세요. 기존 내용은 유지됩니다.', fields, diagnostics=diagnostic)
            if 'source_condition_not_preserved' in reasons:
                return hold('source_conditions_incomplete',
                            '인용 근거는 확인했지만 원문의 신청·제한 조건이 충분히 반영되었는지 '
                            '확인하지 못해 보류했습니다. 지원 내용·신청 자격·나이·소득 항목을 '
                            '원문 전체와 대조해주세요. 기존 내용은 유지됩니다.', fields, diagnostics=diagnostic)
            if reasons & {'number_not_supported', 'quantity_unit_changed', 'label_quantity_changed'}:
                code, reason = 'source_fact_changed', '인용과 표시 내용의 숫자·단위·대상별 수량이 일치하지 않아'
            elif 'source_quote_not_found' in reasons:
                code, reason = 'source_evidence_mismatch', '인용 구간을 원문에서 찾지 못해'
            elif 'source_quote_boundary_changed' in reasons:
                code, reason = 'source_evidence_boundary_changed', '인용의 문장 경계·제한 조건이 원문과 달라'
            elif 'presentation_not_preserved' in reasons:
                code, reason = 'source_presentation_mismatch', '인용과 표시 내용의 문장·조건을 그대로 보존했는지 확인하지 못해'
            else:
                code, reason = 'source_evidence_missing', '정책 본문 또는 충분한 인용 근거를 확인하지 못해'
            labels = {'support_detail': '지원 내용', 'eligibility_detail': '신청 자격',
                      'age_detail': '나이', 'target_income': '소득'}
            field_note = '·'.join(labels[key] for key in fields if key in labels)
            return hold(code, f'{reason} 보류했습니다. '
                        + (f'{field_note} 항목을 원문과 대조해주세요. ' if field_note else '원문을 확인해주세요. ')
                        + '기존 내용은 유지됩니다.', fields, diagnostics=diagnostic)
        # New-call completeness is separate from stored-proposal evidence.
        # Never fill a missing field or reclassify old proposals automatically.
        from services.policy_field_requirements import policy_field_requirements, missing_policy_fields
        missing_fields = []
        if raw_selection is not None:
            requirements = await asyncio.to_thread(policy_field_requirements, content)
            missing_fields = missing_policy_fields(result, requirements)
            from services.policy_source_plan import _source_screened_options
            from services.policy_target_completeness import target_evidence_candidates, missing_target_groups
            screened = await asyncio.to_thread(_source_screened_options, content, allowed)
            candidates = await asyncio.to_thread(target_evidence_candidates, content, screened)
            if missing_target_groups(result, candidates):
                missing_fields.append('target_characteristics')
        if missing_fields:
            labels = {'contact': '연락처 전체', 'target_income': '소득 조건', 'age_detail': '나이 상세 조건',
                      'target_characteristics': '대상 검색 분류'}
            field_note = '·'.join(labels[key] for key in missing_fields)
            return hold('source_field_incomplete',
                        f'원문에 명시된 {field_note}이 해당 전용 항목에 충분히 반영되지 않아 보류했습니다. '
                        '원문 전체와 대조해주세요. 자동 보충이나 추가 요청하지 않았으며 기존 내용은 유지됩니다.',
                        missing_fields)
        # Retain legacy prose/range evidence compatibility. A new unit response
        # must also match its request-specific schema; never repair enum values.
        if raw_selection is not None and not policy_response_matches_schema(raw_selection, response_schema):
            return hold('source_schema_invalid',
                        'AI 응답의 필드 형식 또는 허용된 선택지가 일치하지 않아 보류했습니다. '
                        '추가 요청하지 않았으며 기존 내용은 유지됩니다.')
        if income_review:
            review.update(code='draft_income_review_required',
                          note='소득 기준이 여러 개입니다. 급여·증명서 발급 등 적용 대상과 목적을 원문에서 확인해주세요.',
                          fields=['target_income'])
        return result
    except AiInputReviewRequired as error:
        if draft_preview is True:
            labels = {'identity_number': '개인 식별번호', 'mobile_number': '휴대전화 번호',
                      'email_address': '이메일 주소', 'credential': '비밀키·인증정보',
                      'uninspected_input': '확인할 수 없는 입력'}
            reasons = '·'.join(labels[key] for key in error.reasons if key in labels)
            return hold('ai_input_review_required',
                        f'{reasons or "민감정보"}로 보이는 내용이 있어 AI에 보내지 않았습니다. '
                        '정책 본문에 해당 정보가 있는지 확인해주세요. 같은 주소의 반복 시도로 해결되지 않습니다.')
        return hold('ai_input_review_required', REVIEW_MESSAGE)
    except AiResponseReviewRequired as error:
        return hold(error.code, str(error) + ' 기존 내용은 유지됩니다.')
    except Exception as e:
        from services.ai_coordinator import AiInputTokenBudgetExceeded, AiTemporaryRateLimit, AiQuotaStateUnavailable
        if isinstance(e, AiTemporaryRateLimit):
            return hold('source_input_rate_limited',
                        'AI 요청이 일시적으로 제한되어 분석을 잠시 보류했습니다. '
                        '잠시 후 다시 점검하면 완료하지 못한 정책을 재시도합니다. 기존 내용은 유지됩니다.',
                        retryable=True)
        if draft_preview is True and isinstance(e, AiInputTokenBudgetExceeded):
            return hold('draft_input_budget_exceeded',
                        '분석 요청이 현재 AI 전송 보호값보다 커서 보내지 않았습니다. '
                        '해당 정책만 있는 상세 주소를 사용하거나 설정에서 AI Studio의 실제 한도를 확인해주세요. '
                        '기다렸다가 같은 입력으로 재시도해도 해결되지 않습니다.')
        if isinstance(e, AiInputTokenBudgetExceeded):
            return hold('source_input_budget_exceeded',
                        '분석 직전 확인한 AI 입력 한도를 넘어 전송을 보류했습니다. '
                        '현재 입력 한도를 확인한 뒤 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.')
        if isinstance(e, AiQuotaStateUnavailable):
            return hold('source_input_count_unavailable',
                        'AI 사용량 정보를 확인하지 못해 전송을 잠시 보류했습니다. '
                        '연결·사용 한도를 확인한 뒤 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.',
                        retryable=True)
        logger.error(f"정책 추출 실패 '{title}': {e}")
        return None

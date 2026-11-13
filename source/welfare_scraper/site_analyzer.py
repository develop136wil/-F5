"""
Site Analyzer - AI-powered CSS Selector Detection
Uses Google Gemini API to automatically analyze webpage structure
and suggest CSS selectors for scraping.
"""

import os
import re
import time
import json
import random
import asyncio
from typing import Dict, Any, Optional
from bs4 import BeautifulSoup
from services.logger import setup_logger
from services.ai_generation_safety import (
    AiResponseReviewRequired, SELECTOR_OUTPUT_TOKENS, complete_response_text,
)

logger = setup_logger(__name__)


class _GeminiRateLimiter:
    """
    Gemini Free Tier 슬라이딩 윈도우 Rate Limiter (site_analyzer 전용).

    분당 max_calls 건 이하로 Gemini API 호출을 제한한다.
    기본값: 분당 4건 (무료 티어 한도 5건/분 대비 1건 여유).
    asyncio.Lock은 첫 acquire() 호출 시 생성된다 (이벤트 루프 안전).
    """

    def __init__(self, max_calls: int = 4, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._lock: Optional[asyncio.Lock] = None
        self._call_times: list = []
        self._last_wait_log = 0.0  # 대기 로그 스로틀용

    async def acquire(self):
        """Rate limit 범위 내에서 요청을 허용. 초과 시 자동 대기.

        Lock을 보유한 채 sleep하면 다른 모든 대기자를 차단하므로,
        대기 시간 계산 → Lock 해제 → sleep → Lock 재획득 → 기록 추가 순서로 처리한다.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
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
                # (2026-07-24) 대기 로그 스로틀 — 동시 대기자가 많아도 5초에 한 줄만.
                if now - self._last_wait_log > 5.0:
                    self._last_wait_log = now
                    logger.info(
                        f"[Rate Limiter] Gemini 분당 {self.max_calls}건 한도 도달 — "
                        f"약 {wait_time:.0f}초 대기 후 재개"
                    )
                await asyncio.sleep(wait_time)
            # sleep 후 루프 재진입하여 조건 재확인


# 모듈 단위 싱글톤 — 모든 analyze_with_ai() 호출이 공유
# 기본값(분당 4건)은 클래스 docstring이 명시한 의도와 동일. Google이 모델별 고정
# RPM 표를 더 이상 공개하지 않아 정확한 무료 한도를 코드에서 확정할 수 없으므로,
# 실제 계정 한도(aistudio.google.com/rate-limit)를 확인한 뒤 필요시 env로 조정한다.
_site_rate_limiter = _GeminiRateLimiter(
    max_calls=int(os.getenv('SITE_RATE_MAX_CALLS', '4')),
    period=float(os.getenv('SITE_RATE_PERIOD_SEC', '60.0')),
)

# Load environment variables from parent directory
import utils

# Load environment using centralized utility
utils.load_environment()

def get_genai_client():
    from google import genai
    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key or api_key == '여기에_API_키를_붙여넣으세요':
        return None
    from services import ai_coordinator
    return ai_coordinator.create_client(api_key)

class SiteAnalyzerService:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self._is_running = False

    async def start(self):
        """Starts the browser session."""
        if self._is_running:
            return

        try:
            from playwright.async_api import async_playwright
            from services.public_transport import new_public_context
            logger.info("Initializing Playwright in SiteAnalyzer (STARTING WAIT)...")
            # Prevent indefinite hang during PyInstaller execution
            self.playwright = await asyncio.wait_for(async_playwright().start(), timeout=15.0)
            
            logger.info("Launching System Edge Browser (Headless)...")
            try:
                self.browser = await asyncio.wait_for(
                    self.playwright.chromium.launch(headless=True, channel="msedge"),
                    timeout=30.0
                )
            except Exception as e:
                logger.warning(f"Edge launch failed: {e}. Trying installed Chromium...")
                try:
                    self.browser = await asyncio.wait_for(
                        self.playwright.chromium.launch(headless=True), timeout=30.0
                    )
                except Exception as chromium_error:
                    logger.warning(f"Chromium launch failed: {chromium_error}. Attempting auto-install...")
                    from scraper_engine import ScraperEngine
                    await ScraperEngine._install_browser()
                    # The installer downloads Playwright Chromium, not Microsoft Edge.
                    self.browser = await asyncio.wait_for(
                        self.playwright.chromium.launch(headless=True), timeout=30.0
                    )
            
            # Create context with security bypass
            self.context = await new_public_context(self.browser,
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
            self._is_running = True
            logger.info("SiteAnalyzerService started successfully.")
            
        except asyncio.TimeoutError:
            logger.error("Browser launch timed out. Please ensure Playwright browsers are installed.")
            await self.stop()
            raise Exception("Browser Launch Timeout")
        except Exception as e:
            logger.error(f"Failed to start SiteAnalyzerService: {e}")
            await self.stop()
            raise e

    async def stop(self):
        """Stops the browser session."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        
        self._is_running = False
        self.context = None
        self.browser = None
        self.playwright = None
        logger.info("SiteAnalyzerService stopped.")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def fetch_html(self, url: str, timeout: int = 30000) -> Optional[str]:
        """Fetch HTML content using the shared browser context."""
        if not self._is_running or not self.context:
            await self.start()

        page = None
        try:
            page = await self.context.new_page()
            await page.goto(url, timeout=timeout)
            await page.wait_for_timeout(3000)  # Wait for JS
            html = await page.content()
            return html
        except Exception as e:
            logger.error(f"Failed to fetch HTML from {url}: {e}")
            return None
        finally:
            if page:
                await page.close()

    async def analyze_site(self, url: str) -> Dict[str, Any]:
        """Main method to analyze a site."""
        # Step 1: Fetch HTML
        html = await self.fetch_html(url)
        if not html:
            return {
                "success": False,
                "error": "페이지를 불러올 수 없습니다. URL을 확인해주세요."
            }
        
        # Step 2: Analyze with AI (async)
        # We can reuse the existing analyze_with_ai function or move it here. 
        # For now, let's continually use the standalone function as it's pure logic.
        result = await analyze_with_ai(html, url)
        
        # Step 3: Extract base URL
        from urllib.parse import urlparse
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        result['base_url'] = base_url
        
        return result

# --- Standalone Functions (Refactored to use Service or Helper) ---

def truncate_html(html: str, max_chars: int = 15000) -> str:
    """Truncate HTML to fit within token limits while keeping structure."""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove scripts, styles, and other non-essential elements
    for tag in soup(['script', 'style', 'meta', 'link', 'noscript', 'iframe', 'svg']):
        tag.decompose()
    
    # Get cleaned HTML
    cleaned = str(soup)
    
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + "\n<!-- ... truncated ... -->"
    return cleaned

async def analyze_with_ai(html: str, url: str) -> Dict[str, Any]:
    """Use Gemini AI to analyze HTML and find selectors (async)."""
    client = get_genai_client()
    if not client:
        return {
            "success": False,
            "error": "API 키가 설정되지 않았습니다. .env 파일에 GOOGLE_API_KEY를 입력해주세요."
        }
    
    truncated_html = truncate_html(html)
    
    prompt = f"""당신은 웹 스크레이핑 전문가입니다. 아래 HTML은 공지사항/게시판 페이지입니다.

URL: {url}

HTML:
```html
{truncated_html}
```

이 페이지에서 게시글 목록을 추출하기 위한 CSS 선택자를 찾아주세요.

다음 형식의 JSON으로만 응답해주세요 (다른 텍스트 없이):
{{
    "list_item": "게시글 한 줄(row)을 선택하는 CSS 선택자 (예: table.boardList tbody tr)",
    "title": "제목 링크를 선택하는 CSS 선택자 (예: td.al a)",
    "date": "날짜를 선택하는 CSS 선택자 (예: td:nth-child(3))",
    "link": "링크를 선택하는 CSS 선택자 (보통 title과 동일)",
    "confidence": "높음/중간/낮음 중 하나",
    "explanation": "선택한 이유 간단 설명"
}}

중요:
- list_item은 반드시 tbody 안의 tr만 선택해야 합니다 (thead 제외)
- 실제 HTML 구조를 분석하여 정확한 선택자를 제시하세요
- 확신이 없으면 confidence를 "낮음"으로 설정하세요
"""

    from services.ai_input_safety import ensure_ai_text_safe, AiInputReviewRequired
    try:
        ensure_ai_text_safe(prompt)
    except AiInputReviewRequired as error:
        return {'success': False, 'error': str(error), 'code': error.code}
    # Conservative site queue; actual free limits depend on the project.
    await _site_rate_limiter.acquire()

    from google.genai import types

    # 429 RESOURCE_EXHAUSTED 시 retryDelay 준수 후 최대 3회 재시도
    _MAX_RETRIES = 3
    response_text = None
    model_name = os.getenv('SITE_ANALYZE_MODEL', 'gemini-3.5-flash-lite')
    for _attempt in range(_MAX_RETRIES):
        try:
            from services import ai_coordinator
            await ai_coordinator.acquire_model_slot(model_name,
                input_tokens=ai_coordinator.estimate_input_tokens(prompt))
            response = await ai_coordinator.await_api(client.aio.models.generate_content(
                # Site analysis shares the app's request budget. Model names
                # are not proof of a particular free quota.
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json', temperature=0,
                    max_output_tokens=SELECTOR_OUTPUT_TOKENS, candidate_count=1,
                )
            ))

            # Parse JSON from response
            response_text = complete_response_text(response)

            # (하드닝 2026-07-23) 펜스 제거를 슬라이싱→정규식으로 (ai_classifier와 동일).
            # "``` json"·"```JSON"처럼 한 글자만 어긋나도 슬라이싱은 엉뚱한 위치를 잘랐다.
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                _t = re.sub(r'^```\s*(?:json)?\s*', '', response_text, flags=re.IGNORECASE)
                _t = re.sub(r'\s*```$', '', _t)
                result = json.loads(_t)
            if (not isinstance(result, dict)
                    or any(not isinstance(result.get(key), str) or not result[key].strip()
                           for key in ('list_item', 'title'))
                    or any(key in result and not isinstance(result[key], str) for key in ('date', 'link'))):
                raise AiResponseReviewRequired('incomplete')
            result['success'] = True
            return result

        except AiResponseReviewRequired as error:
            return {'success': False, 'error': str(error), 'code': error.code,
                    'needs_source_review': True}
        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"AI 응답 파싱 실패: {str(e)}",
                "raw_response": response_text
            }
        except Exception as _e:
            _err_str = str(_e)
            _error_info = ai_coordinator.register_api_error(model_name, _e)
            if _error_info.get('local_budget_exhausted'):
                return {'success': False, 'error': str(_e), 'code': 'AI_LOCAL_BUDGET_EXHAUSTED'}
            # Provider daily exhaustion: no automatic retry before Pacific reset.
            if _error_info['daily_exhausted']:
                logger.error(
                    "🚫 [일일 한도 소진] 무료 등급 사이트 분석 모델의 일일 요청 한도 초과. "
                    "태평양 시간 자정 이후 한도를 다시 확인할 수 있습니다."
                )
                return {
                    "success": False,
                    "error": "일일 AI 분석 한도가 소진되었습니다. 태평양 시간 자정 이후 자동으로 다시 사용할 수 있습니다."
                }
            # 분당 한도 — retryDelay 파싱 후 재시도
            # 단따옴표/쌍따옴표 모두 지원 (Python str() 변환 시 단따옴표 사용)
            if _error_info['rate_limited'] and _attempt < _MAX_RETRIES - 1:
                # 동시 재시도(thundering herd) 방지용 지터 — ai_classifier.py와 동일 원칙
                _wait = (_error_info['retry_after'] + 2 if _error_info['retry_after'] else 30) + random.uniform(0, 5)
                ai_coordinator.set_model_cooldown(model_name, _wait)
                logger.warning(
                    f"⏳ [429] Gemini 분당 한도 감지 — {_wait:.0f}초 후 재시도... "
                    f"(시도 {_attempt + 1}/{_MAX_RETRIES}) | {url}"
                )
                await asyncio.sleep(_wait)
                continue
            # 기타 에러
            return {
                "success": False,
                "error": f"AI 분석 중 오류: {_err_str}"
            }

    return {"success": False, "error": "Gemini API 최대 재시도 횟수 초과 (429 반복)"}

async def analyze_site(url: str) -> Dict[str, Any]:
    """
    Main function to analyze a site and return selector suggestions.
    Legacy wrapper for backward compatibility. Use SiteAnalyzerService for batch ops.
    """
    async with SiteAnalyzerService() as service:
        return await service.analyze_site(url)

# Manual guide for fallback
MANUAL_GUIDE = """
## 🔍 CSS 선택자 직접 찾는 방법

1. **브라우저에서 해당 페이지 열기**
2. **F12 키를 눌러 개발자 도구 열기**
3. **게시글 목록 찾기**:
   - 좌측 상단의 화살표 아이콘(요소 선택) 클릭
   - 게시글 한 줄 위로 마우스 이동 후 클릭
   - Elements 탭에서 해당 요소 확인

4. **선택자 찾기**:
   - 목록 선택자: 보통 `table tbody tr` 또는 `ul.board-list li`
   - 제목 선택자: 보통 `td.title a` 또는 `td.subject a`
   - 날짜 선택자: 보통 `td:nth-child(3)` 또는 `td.date`

5. **테스트하기**:
   - 콘솔 탭에서 `document.querySelectorAll('선택자')` 입력
   - 게시글 개수만큼 요소가 선택되면 성공!
"""

if __name__ == "__main__":
    # Test
    import asyncio
    test_url = "https://health.dobong.go.kr/bbs.asp?code=10005006"
    result = asyncio.run(analyze_site(test_url))
    print(json.dumps(result, ensure_ascii=False, indent=2))

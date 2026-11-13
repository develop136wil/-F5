import asyncio
import os
import datetime
import html as html_module
import aiohttp
from typing import List, Dict, Any, Optional
from plyer import notification

import utils
from services.logger import setup_logger
from services.temp_files import create_temp_file
from services.age_values import month_value

utils.load_environment()
logger = setup_logger(__name__)


def _md_escape(text: str) -> str:
    """마크다운 링크 텍스트에서 깨질 수 있는 문자를 이스케이프."""
    return text.replace('[', '\\[').replace(']', '\\]').replace('(', '\\(').replace(')', '\\)')

def send_notification(count: int) -> None:
    """
    Sends a desktop notification.
    """
    try:
        notification.notify(
            title='영유아 복지 정보 알림',
            message=f'새로운 복지 정보가 {count}건 수집되었습니다.',
            app_name='Welfare Scraper',
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Notification failed: {e}")

def _parse_item_data(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    ai_data JSON 파싱 + 공통 속성 추출.
    generate_notion_report()와 generate_notion_markdown_report()에서 공유.
    """
    from utils import parse_json_safe
    ai = parse_json_safe(item.get('ai_data'), {})

    tc_raw = ai.get('target_characteristics') or item.get('target_characteristics', '')
    tc_list = tc_raw if isinstance(tc_raw, list) else [t.strip() for t in tc_raw.split(',') if t.strip()] if tc_raw else []

    lc_raw = ai.get('life_cycle') or item.get('life_cycle', '')
    lc_list = lc_raw if isinstance(lc_raw, list) else [l.strip() for l in lc_raw.split(',') if l.strip()] if lc_raw else []

    res_raw = ai.get('residence') or item.get('residence', '')
    res_list = res_raw if isinstance(res_raw, list) else [r.strip() for r in res_raw.split(',') if r.strip()] if res_raw else []

    # 출처 기관: AI가 감지한 원본 기관명 우선, 없으면 사이트명
    source_org = ai.get('source_site_name') or item.get('site_name', '')

    # 관련 홈페이지: ai_data의 related_urls (리스트 or DB에서 직렬화된 쉼표 문자열 모두 처리)
    ru_raw = ai.get('related_urls') or item.get('related_urls', '')
    if isinstance(ru_raw, list):
        related_urls = [u.strip() for u in ru_raw if isinstance(u, str) and u.strip()]
    elif isinstance(ru_raw, str) and ru_raw.strip():
        related_urls = [u.strip() for u in ru_raw.split(',') if u.strip()]
    else:
        related_urls = []

    return {
        'ai': ai,
        'tc_list': tc_list,
        'lc_list': lc_list,
        'res_list': res_list,
        'start_age': month_value(ai.get('start_age_months', item.get('start_age_months', ''))),
        'end_age': month_value(ai.get('end_age_months', item.get('end_age_months', ''))),
        'age_detail': ai.get('age_detail', item.get('age_detail', '')) or '-',
        'income': ai.get('target_income', item.get('target_income', '')) or '-',
        'eligibility': ai.get('eligibility_detail', item.get('eligibility_detail', '')) or '-',
        'contact': ai.get('contact', item.get('contact', '')) or '-',
        'support': ai.get('support_detail', item.get('support_detail', '')) or '',
        'deadline': item.get('deadline') or '-',
        'event_date': item.get('event_date') or '',
        'category': item.get('category') or '기타',
        'site_name': item.get('site_name', ''),
        'source_org': source_org,
        'related_urls': related_urls,
        # program: 자격 요건 있는 복지 사업 / event: 단기 소식·이벤트
        'item_type': ai.get('item_type', 'program'),
    }

async def send_discord_notification(items: List[Dict[str, Any]], session: Optional[aiohttp.ClientSession] = None) -> None:
    """
    Sends a notification to Discord via Webhook (async).
    session 파라미터를 전달하면 재사용, 없으면 내부에서 생성.

    환경변수 필터 (선택, 미설정 시 전체 항목 전송):
      DISCORD_FILTER_CATEGORIES  쉼표 구분 카테고리 화이트리스트 (예: "교육/보육,의료/재활")
      DISCORD_FILTER_TARGETS     쉼표 구분 대상 특성 화이트리스트 (예: "장애/발달지연,저소득")
    두 필터가 모두 설정된 경우 OR 조건으로 매칭 (어느 한 조건을 만족하면 포함).
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    # ── 카테고리·대상 필터 적용 ──────────────────────────────────────────
    filter_cats = {c.strip() for c in os.getenv('DISCORD_FILTER_CATEGORIES', '').split(',') if c.strip()}
    filter_tgts = {t.strip() for t in os.getenv('DISCORD_FILTER_TARGETS', '').split(',') if t.strip()}

    if filter_cats or filter_tgts:
        def _item_matches(item: Dict[str, Any]) -> bool:
            if filter_cats and item.get('category', '') in filter_cats:
                return True
            if filter_tgts:
                tc_raw = item.get('target_characteristics', '')
                if tc_raw:
                    tc_set = set(tc_raw) if isinstance(tc_raw, list) else {
                        t.strip() for t in tc_raw.split(',') if t.strip()
                    }
                    if tc_set & filter_tgts:
                        return True
            return False

        original_count = len(items)
        items = [i for i in items if _item_matches(i)]
        if len(items) < original_count:
            logger.info(
                f"🔕 [Discord 필터] {original_count - len(items)}건 제외 "
                f"(카테고리={filter_cats or '전체'}, 대상={filter_tgts or '전체'}) "
                f"→ {len(items)}건 전송"
            )
        if not items:
            logger.info("ℹ️ [Discord] 필터 조건에 맞는 항목이 없어 알림을 건너뜁니다.")
            return
    # ────────────────────────────────────────────────────────────────────

    try:
        embeds = []
        embeds.append({
            "title": f"📢 새로운 복지 정보 {len(items)}건 발견!",
            "description": "아래 새로운 정보들이 수집되었습니다.",
            "color": 5814783
        })
        for item in items[:5]:
            category = item.get('category', '공지')
            target = item.get('target', '일반')
            embeds.append({
                "title": f"[{category}] {item['title']}",
                "url": item['link'],
                "description": f"사이트: {item['site_name']} | 대상: {target}",
                "color": 3447003
            })
        if len(items) > 5:
            embeds.append({
                "title": f"...외 {len(items)-5}건 더 있음",
                "description": "전체 목록은 리포트를 확인하세요.",
                "color": 15158332
            })

        payload = {"username": "복지 정보 알리미", "embeds": embeds}

        async def _post(s):
            for attempt in range(3):
                try:
                    async with s.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if not (200 <= response.status < 300):
                            if response.status == 429 and attempt < 2:
                                retry_after = float(response.headers.get('Retry-After', '2'))
                                await asyncio.sleep(min(retry_after, 5))
                                continue
                            logger.warning(f"Discord notification failed: {response.status}")
                        return
                except aiohttp.ClientError as exc:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    logger.warning(f"Discord notification failed after retries: {exc}")
                    return

        if session:
            await _post(session)
        else:
            async with aiohttp.ClientSession() as s:
                await _post(s)

    except Exception as e:
        logger.error(f"Discord notification error: {e}")

async def send_google_chat_notification(items: List[Dict[str, Any]], session: Optional[aiohttp.ClientSession] = None) -> None:
    """Google Chat Incoming Webhook으로 새 항목 알림 (async).

    환경변수 GOOGLE_CHAT_WEBHOOK_URL 설정 시 활성화. Discord와 동일하게
    DISCORD_FILTER_CATEGORIES / DISCORD_FILTER_TARGETS 필터를 공유 적용한다.
    Google Chat 텍스트 포맷: *굵게*, <URL|표시텍스트> 링크.
    """
    webhook_url = os.getenv("GOOGLE_CHAT_WEBHOOK_URL")
    if not webhook_url:
        return

    # Discord와 동일한 카테고리·대상 필터 공유
    filter_cats = {c.strip() for c in os.getenv('DISCORD_FILTER_CATEGORIES', '').split(',') if c.strip()}
    filter_tgts = {t.strip() for t in os.getenv('DISCORD_FILTER_TARGETS', '').split(',') if t.strip()}
    if filter_cats or filter_tgts:
        def _match(item: Dict[str, Any]) -> bool:
            if filter_cats and item.get('category', '') in filter_cats:
                return True
            if filter_tgts:
                tc_raw = item.get('target_characteristics', '')
                if tc_raw:
                    tc_set = set(tc_raw) if isinstance(tc_raw, list) else {t.strip() for t in tc_raw.split(',') if t.strip()}
                    if tc_set & filter_tgts:
                        return True
            return False
        items = [i for i in items if _match(i)]
        if not items:
            logger.info("ℹ️ [Google Chat] 필터 조건에 맞는 항목이 없어 알림을 건너뜁니다.")
            return

    if not items:
        return

    lines = [f"*📢 새로운 복지 정보 {len(items)}건 발견!*", ""]
    for item in items[:10]:
        category = item.get('category', '공지')
        site = item.get('site_name', '')
        title = (item.get('title', '') or '')[:80]
        link = item.get('link', '')
        if link and not str(link).lower().startswith('javascript:'):
            lines.append(f"• [{category}] <{link}|{title}> — {site}")
        else:
            lines.append(f"• [{category}] {title} — {site}")
    if len(items) > 10:
        lines.append(f"…외 {len(items) - 10}건 (전체는 리포트 확인)")

    content = '\n'.join(lines)
    if len(content) > 3900:  # Google Chat 텍스트 한도 보호
        content = content[:3900] + "\n…"

    async def _post(s):
        for attempt in range(3):
            try:
                async with s.post(webhook_url, json={"text": content}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status not in (200, 204):
                        if resp.status == 429 and attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        logger.warning(f"Google Chat notification failed: {resp.status}")
                    return
            except aiohttp.ClientError as exc:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                logger.warning(f"Google Chat notification failed after retries: {exc}")
                return

    try:
        if session:
            await _post(session)
        else:
            async with aiohttp.ClientSession() as s:
                await _post(s)
    except Exception as e:
        logger.error(f"Google Chat notification error: {e}")


async def send_google_chat_text(text: str, session: Optional[aiohttp.ClientSession] = None) -> None:
    """임의 텍스트를 Google Chat으로 전송 (정책 최신화 점검 요약 등)."""
    webhook_url = os.getenv("GOOGLE_CHAT_WEBHOOK_URL")
    if not webhook_url or not text:
        return
    content = text[:3900]

    async def _post(s):
        try:
            async with s.post(webhook_url, json={"text": content},
                              timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status not in (200, 204):
                    logger.warning(f"Google Chat text failed: {resp.status}")
        except Exception as exc:
            logger.warning(f"Google Chat text error: {exc}")

    try:
        if session:
            await _post(session)
        else:
            async with aiohttp.ClientSession() as s:
                await _post(s)
    except Exception as e:
        logger.error(f"Google Chat text error: {e}")


def generate_report(items: List[Dict[str, Any]]) -> str:
    """
    Generates an HTML report for the user to review.
    """
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    fd, filepath = create_temp_file(suffix='.html', prefix=f'Report_{date_str}_')
    os.close(fd)
    
    html_content = f"""
    <html>
    <head>
        <title>영유아 복지 정보 리포트 ({date_str})</title>
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; padding: 20px; }}
            h1 {{ color: #2c3e50; }}
            .item {{ border: 1px solid #ddd; padding: 15px; margin-bottom: 10px; border-radius: 5px; }}
            .category {{ font-weight: bold; color: #e67e22; }}
            .title {{ font-size: 1.2em; font-weight: bold; margin: 5px 0; }}
            .date {{ color: #7f8c8d; font-size: 0.9em; }}
            a {{ text-decoration: none; color: #3498db; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h1>📅 {date_str} 수집 리포트</h1>
        <p>총 {len(items)}건의 새로운 정보가 발견되었습니다.</p>
        <hr>
    """
    
    for item in items:
        safe_title = html_module.escape(item.get('title', ''))
        _raw_link = item.get('link', '')
        safe_link = '#' if _raw_link.lower().startswith('javascript:') else html_module.escape(_raw_link)
        safe_site = html_module.escape(item.get('site_name', ''))
        safe_category = html_module.escape(item.get('category', '공지'))
        safe_date = html_module.escape(item.get('date', ''))
        html_content += f"""
        <div class="item">
            <div class="category">{safe_category}</div>
            <div class="title"><a href="{safe_link}" target="_blank">{safe_title}</a></div>
            <div class="date">{safe_site} | {safe_date}</div>
        </div>
        """
        
    html_content += """
    </body>
    </html>
    """
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)
        
    return filepath

def generate_notion_report(items: List[Dict[str, Any]]) -> str:
    """
    Generates a Notion-style HTML report matching the user's Notion DB layout.
    Each item is rendered with property rows + rich text detail.
    """
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    fd, filepath = create_temp_file(suffix='.html', prefix=f'NotionReport_{date_str}_')
    os.close(fd)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>복지 정보 Notion 리포트 ({date_str})</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Pretendard', 'Malgun Gothic', sans-serif; background: #f7f7f5; color: #37352f; padding: 40px; }}
    .report-header {{ max-width: 900px; margin: 0 auto 30px; }}
    .report-header h1 {{ font-size: 28px; margin-bottom: 8px; }}
    .report-header p {{ color: #787774; font-size: 14px; }}
    .card {{ max-width: 900px; margin: 0 auto 24px; background: #fff; border-radius: 8px; border: 1px solid #e3e3e0; overflow: hidden; }}
    .card-title {{ font-size: 22px; font-weight: 700; padding: 20px 24px 12px; border-bottom: 1px solid #f0f0ee; }}
    .card-title a {{ color: inherit; text-decoration: none; }}
    .card-title a:hover {{ text-decoration: underline; color: #2383e2; }}
    .props {{ padding: 0; }}
    .prop {{ display: flex; border-bottom: 1px solid #f0f0ee; min-height: 34px; }}
    .prop-icon {{ width: 28px; display: flex; align-items: center; justify-content: center; color: #9b9a97; font-size: 12px; padding-left: 10px; flex-shrink: 0; }}
    .prop-label {{ width: 140px; padding: 8px 8px; font-size: 13px; color: #787774; flex-shrink: 0; display: flex; align-items: center; }}
    .prop-value {{ flex: 1; padding: 8px 12px; font-size: 14px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }}
    .tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 500; }}
    .tag-blue {{ background: #d3e5ef; color: #183b56; }}
    .tag-green {{ background: #dbeddb; color: #1a4d1a; }}
    .tag-red {{ background: #ffe2dd; color: #5a1a1a; }}
    .tag-purple {{ background: #e8deee; color: #412b54; }}
    .tag-yellow {{ background: #fdecc8; color: #5c3b00; }}
    .tag-pink {{ background: #f5e0e9; color: #5a1a3a; }}
    .tag-gray {{ background: #e3e2e0; color: #37352f; }}
    .detail-section {{ padding: 16px 24px 20px; border-top: 1px solid #f0f0ee; font-size: 14px; line-height: 1.7; white-space: pre-wrap; }}
    .detail-section h4 {{ margin-top: 12px; font-size: 14px; }}
    .separator {{ border: none; border-top: 1px solid #e3e3e0; margin: 0; }}
</style>
</head>
<body>
<div class="report-header">
    <h1>📋 복지 정보 리포트</h1>
    <p>{date_str} · {len(items)}건</p>
</div>
"""

    category_colors = {
        "의료/재활": "blue", "교육/보육": "green", "가족지원": "pink",
        "돌봄/양육": "purple", "복지": "yellow", "생활지원": "yellow", "기타": "gray"
    }
    target_colors = {
        "일반": "gray", "장애/발달지연": "purple", "저소득": "red",
        "임산부": "pink", "보호자": "blue", "다문화": "green",
        "다자녀": "yellow", "한부모": "red"
    }
    lifecycle_colors = {
        "임신/출산": "pink", "영아": "blue", "유아": "green",
        "학령기": "yellow", "청소년": "purple", "전생애": "gray"
    }

    for item in items:
        d = _parse_item_data(item)
        category = d['category']
        cat_color = category_colors.get(category, 'gray')

        safe_title = html_module.escape(item.get('title', ''))
        _raw_link2 = item.get('link', '')
        safe_link = '#' if _raw_link2.lower().startswith('javascript:') else html_module.escape(_raw_link2)

        tc_html = ''.join(f'<span class="tag tag-{target_colors.get(t, "gray")}">{html_module.escape(t)}</span>' for t in d['tc_list']) or '-'
        res_html = ''.join(f'<span class="tag tag-blue">{html_module.escape(r)}</span>' for r in d['res_list']) or '-'
        lc_html = ''.join(f'<span class="tag tag-{lifecycle_colors.get(l, "gray")}">{html_module.escape(l)}</span>' for l in d['lc_list']) or '-'

        start_age = html_module.escape(str(d['start_age'])) if d['start_age'] is not None else None
        end_age = html_module.escape(str(d['end_age'])) if d['end_age'] is not None else None
        age_detail = d['age_detail']
        income = d['income']
        eligibility = d['eligibility']
        contact = d['contact']
        support = d['support']
        deadline = d['deadline']

        html += f"""
<div class="card">
    <div class="card-title"><a href="{safe_link}" target="_blank">{safe_title}</a></div>
    <div class="props">
        <div class="prop"><span class="prop-icon">◉</span><span class="prop-label">분류</span><span class="prop-value"><span class="tag tag-{cat_color}">{html_module.escape(category)}</span></span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">대상 특성</span><span class="prop-value">{tc_html}</span></div>
        <div class="prop"><span class="prop-icon">#</span><span class="prop-label">시작 월령(개월)</span><span class="prop-value">{start_age if start_age is not None and start_age != '' else '-'}</span></div>
        <div class="prop"><span class="prop-icon">#</span><span class="prop-label">종료 월령(개월)</span><span class="prop-value">{end_age if end_age is not None and end_age != '' else '-'}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">세부 연령</span><span class="prop-value">{html_module.escape(str(age_detail))}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">대상 소득 수준</span><span class="prop-value">{html_module.escape(str(income))}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">추가 자격 요건</span><span class="prop-value">{html_module.escape(str(eligibility))}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">거주지</span><span class="prop-value">{res_html}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">마감일</span><span class="prop-value">{html_module.escape(str(deadline))}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">문의처</span><span class="prop-value">{html_module.escape(str(contact))}</span></div>
        <div class="prop"><span class="prop-icon">≡</span><span class="prop-label">생애주기</span><span class="prop-value">{lc_html}</span></div>
    </div>
    <div class="detail-section">{html_module.escape(str(support))}</div>
</div>
"""

    html += """
</body>
</html>
"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)

    return filepath

def generate_notion_markdown_report(items: List[Dict[str, Any]]) -> str:
    """
    Generates a Notion-ready Markdown report.
    - 각 항목을 독립된 Notion 페이지처럼 구조화
    - 속성 테이블 + ### 섹션 헤딩 + 마감 임박 callout 포함
    - 그대로 복사해서 Notion에 붙여넣기 가능
    """
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    fd, filepath = create_temp_file(suffix='.md', prefix=f'NotionReport_{date_str}_')
    os.close(fd)

    today = datetime.datetime.now().date()

    # item_type 기준으로 분리
    parsed = [(item, _parse_item_data(item)) for item in items]
    programs = [(item, d) for item, d in parsed if d['item_type'] != 'event']
    events   = [(item, d) for item, d in parsed if d['item_type'] == 'event']

    lines = []
    lines.append("# 📋 복지 정보 리포트")
    lines.append("")
    lines.append(
        f"> 🗓️ **생성일**: {date_str}  |  "
        f"📦 **총 {len(items)}건** "
        f"(지원 사업 {len(programs)}건 · 단기 소식 {len(events)}건)"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ══════════════════════════════════════════════════
    # 섹션 1: 단기 소식 / 이벤트 (간략 테이블)
    # ══════════════════════════════════════════════════
    if events:
        lines.append("## 📢 단기 소식 / 이벤트")
        lines.append("")
        lines.append("> 예약 오픈, 특강, 단기 프로그램 등 기한이 중요한 소식입니다.")
        lines.append("")
        lines.append("| 제목 | 기관 | 일정 | 링크 |")
        lines.append("|---|---|---|---|")

        for item, d in events:
            title      = item.get('title', '제목 없음').replace('|', '\\|')
            link       = item.get('link', '')
            source_org = d['source_org'] or d['site_name'] or '-'
            # 이벤트는 event_date 우선, 없으면 deadline
            date_val   = d['event_date'] or d['deadline']
            if not date_val or date_val == '-':
                date_val = '-'

            title_cell = f"[{_md_escape(title)}]({link})" if link else title
            link_cell  = f"[바로가기]({link})" if link else '-'
            lines.append(f"| {title_cell} | {source_org} | {date_val} | {link_cell} |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # ══════════════════════════════════════════════════
    # 섹션 2: 지원 사업 / 복지 프로그램 (상세 카드)
    # ══════════════════════════════════════════════════
    if programs:
        lines.append("## 🏛️ 지원 사업 / 복지 프로그램")
        lines.append("")

    for idx, (item, d) in enumerate(programs, 1):
        title    = item.get('title', '제목 없음')
        link     = item.get('link', '')
        category = d['category']
        site_name = d['site_name']

        tc_str  = ', '.join(d['tc_list']) if d['tc_list'] else '-'
        lc_str  = ', '.join(d['lc_list']) if d['lc_list'] else '-'
        res_str = ', '.join(d['res_list']) if d['res_list'] else '-'

        start_age = d['start_age']
        end_age   = d['end_age']
        age_detail_val = d['age_detail']

        # 월령 개별 표시용
        start_age_str = f"{start_age}개월" if (start_age is not None and start_age != '') else '-'
        end_age_str   = f"{end_age}개월"   if (end_age   is not None and end_age   != '') else '-'
        age_detail_str = age_detail_val if (age_detail_val and age_detail_val != '-') else '-'

        income      = d['income']
        eligibility = d['eligibility']
        contact     = d['contact']
        support     = d['support'] or ''
        deadline    = d['deadline']
        event_date  = d['event_date']

        # 관련 홈페이지 링크 셀 구성
        related_urls = d.get('related_urls', [])
        if related_urls:
            url_links = ' · '.join(f"[관련 {i+1}]({u})" for i, u in enumerate(related_urls))
        else:
            url_links = '-'

        # ── 제목 (링크 포함) ──────────────────────────────────
        if link:
            lines.append(f"### {idx}. [{_md_escape(title)}]({link})")
        else:
            lines.append(f"### {idx}. {title}")
        lines.append("")

        # ── 마감 임박 경고 callout ────────────────────────────
        if deadline and deadline != '-':
            try:
                from services.news_deadline_grounding import deadline_calendar_day
                dl = deadline_calendar_day(deadline)
                days_left = (dl - today).days
                if days_left < 0:
                    lines.append(f"> ⚫ **마감됨** ({deadline})")
                    lines.append("")
                elif days_left <= 7:
                    lines.append(f"> 🔴 **마감 임박 D-{days_left}** ({deadline})")
                    lines.append("")
                elif days_left <= 30:
                    lines.append(f"> 🟡 **마감 {days_left}일 전** ({deadline})")
                    lines.append("")
            except ValueError:
                pass

        # ── 기본 속성 테이블 ──────────────────────────────────
        lines.append("| 속성 | 값 |")
        lines.append("|---|---|")
        lines.append(f"| 🏷️ 분류 | **{category}** |")
        lines.append(f"| 🌐 출처 | {site_name} |")
        lines.append(f"| 👥 대상 특성 | {tc_str} |")
        lines.append(f"| 🎂 시작 월령 | {start_age_str} |")
        lines.append(f"| 🎂 종료 월령 | {end_age_str} |")
        lines.append(f"| 📋 세부 연령 | {age_detail_str} |")
        lines.append(f"| 💴 소득 기준 | {income} |")
        lines.append(f"| 📍 거주지 | {res_str} |")
        lines.append(f"| 🔄 생애주기 | {lc_str} |")
        lines.append(f"| ⏰ 마감일 | {deadline} |")
        if event_date:
            lines.append(f"| 📆 일정 | {event_date} |")
        lines.append(f"| 📞 문의처 | {contact} |")
        lines.append(f"| 📝 자격 요건 | {eligibility} |")
        lines.append(f"| 🔗 관련 홈페이지 | {url_links} |")
        lines.append("")

        # ── 지원 내용 상세 ────────────────────────────────────
        if support:
            lines.append("#### 📄 지원 내용 상세")
            lines.append("")
            lines.append(support)
            lines.append("")

        # ── 원문 링크 ─────────────────────────────────────────
        if link:
            lines.append(f"🔗 **원문**: [{site_name}]({link})")
            lines.append("")

        lines.append("---")
        lines.append("")

    content = '\n'.join(lines)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    return filepath


async def notify_expiring_items(session: Optional[aiohttp.ClientSession] = None) -> None:
    """
    마감 임박(3일, 7일) 항목을 Discord + Google Chat으로 알림.
    스크래핑 완료 후 자동 호출 또는 별도 실행 가능.
    설정된 채널(웹훅 URL)에만 전송한다.
    """
    import history_manager as hm
    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    gchat_url = os.getenv("GOOGLE_CHAT_WEBHOOK_URL")
    if not discord_url and not gchat_url:
        return

    today = datetime.date.today()
    items_7d = hm.get_expiring_items(days=7)
    if not items_7d:
        return

    # 채널별 포맷 차이(Discord: [text](url) / Google Chat: <url|text>)를 위해
    # 공통 필드를 먼저 추출한 뒤 채널에 맞게 렌더링.
    rows = []
    for item in items_7d:
        deadline = item.get('deadline', '')
        try:
            from services.news_deadline_grounding import deadline_calendar_day
            d = deadline_calendar_day(deadline)
            days_left = (d - today).days
            badge = f"D-{days_left}"
            emoji = "🔴" if days_left <= 3 else "🟡"
        except (ValueError, TypeError):
            badge, emoji = "", "📅"
        rows.append({
            "title": (item.get('title', '') or '')[:60],
            "link": item.get('link', ''),
            "site": item.get('site_name', ''),
            "deadline": deadline,
            "badge": badge,
            "emoji": emoji,
        })

    def _discord_content():
        lines = ["## ⏰ 마감 임박 안내", ""]
        for r in rows:
            b = f"{r['emoji']} **{r['badge']}**" if r['badge'] else r['emoji']
            if r['link']:
                lines.append(f"- {b} [{_md_escape(r['title'])}]({r['link']}) _{_md_escape(r['site'])}_ (마감: {r['deadline']})")
            else:
                lines.append(f"- {b} {r['title']} _{r['site']}_ (마감: {r['deadline']})")
        c = '\n'.join(lines)
        return c[:1900] + "\n..." if len(c) > 1900 else c

    def _gchat_content():
        lines = ["*⏰ 마감 임박 안내*", ""]
        for r in rows:
            b = f"{r['emoji']} *{r['badge']}*" if r['badge'] else r['emoji']
            if r['link'] and not str(r['link']).lower().startswith('javascript:'):
                lines.append(f"• {b} <{r['link']}|{r['title']}> — {r['site']} (마감: {r['deadline']})")
            else:
                lines.append(f"• {b} {r['title']} — {r['site']} (마감: {r['deadline']})")
        c = '\n'.join(lines)
        return c[:3900] + "\n…" if len(c) > 3900 else c

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        if discord_url:
            try:
                async with session.post(discord_url, json={"content": _discord_content()}) as resp:
                    if resp.status not in (200, 204):
                        logger.warning(f"Discord deadline notify failed: {resp.status}")
            except Exception as e:
                logger.warning(f"Discord deadline notify error: {e}")
        if gchat_url:
            try:
                async with session.post(gchat_url, json={"text": _gchat_content()}) as resp:
                    if resp.status not in (200, 204):
                        logger.warning(f"Google Chat deadline notify failed: {resp.status}")
            except Exception as e:
                logger.warning(f"Google Chat deadline notify error: {e}")
    finally:
        if own_session:
            await session.close()

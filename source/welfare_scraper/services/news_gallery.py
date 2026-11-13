"""새소식 HTML 갤러리와 포스터 조회 캐시.

HTML 내보내기는 이미 수집·분석된 ``welfare_items``만 소비한다. 이 모듈은 Gemini나
임베딩 서비스를 호출하지 않으며, 포스터 탐색 결과만 별도 테이블에 보관해 같은 원문을
내보낼 때마다 브라우저를 다시 열지 않게 한다.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
from services.temp_files import create_temp_file
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse


POSITIVE_CACHE_DAYS = 30
NEGATIVE_CACHE_DAYS = 7
MAX_GALLERY_BYTES = 20 * 1024 * 1024
DETAIL_FIELDS = (("support_detail", "상세 내용"), ("eligibility_detail", "신청 자격"), ("contact", "문의처"))


class GalleryTooLargeError(ValueError):
    """Never export a silently shortened document or leave a partial file."""


def _details(item: Dict[str, Any]):
    from services.news_display_projection import item_presentation
    display = item_presentation(item)
    fields = dict(item, support_detail=display['support_detail'])
    return [(label, str(fields.get(key) or "").strip()) for key, label in DETAIL_FIELDS
            if str(fields.get(key) or "").strip()]


def _detail_html(item: Dict[str, Any]) -> str:
    sections = _details(item)
    if not sections:
        return '<p class="summary">내용은 원문에서 확인해주세요.</p>'
    # Show complete short content, never a sentence fragment that could hide a condition.
    size = sum(len(value) for _, value in sections)
    lines = sum(len(value.splitlines()) for _, value in sections)
    expanded = ' open' if size <= 600 and lines <= 8 else ''
    from services.news_display_projection import item_presentation
    structured = item_presentation(item)['presentation_kind'] != 'legacy'
    fields = ''.join(f'<dt>{_e(label)}</dt><dd>{(_readable_html(value) if structured else _legacy_markdown_html(value)) if label == "상세 내용" else _e(value)}</dd>'
                     for label, value in sections)
    return (f'<details class="full-content"{expanded}><summary>상세 내용'
            f'<span class="detail-count">{size:,}자 · 펼치기/접기</span></summary>'
            f'<dl>{fields}</dl></details>')


def _readable_html(text):
    """Render only code-owned section titles; all source text stays escaped."""
    headings = {'지원 내용', '대상·자격', '신청 방법', '기간', '참고 사항'}
    parts = []
    for line in text.splitlines():
        if line.startswith('### ') and line[4:] in headings:
            parts.append('<strong>' + _e(line[4:]) + '</strong>')
        else:
            parts.append(_e(line))
    return '\n'.join(parts)


def _markdown_inline(text):
    """Same small inline subset as the app; raw HTML is always escaped."""
    pattern = r'\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s)]+)\)'
    parts, cursor = [], 0
    for match in re.finditer(pattern, text):
        parts.append(_e(text[cursor:match.start()]))
        strong, alternate, code, label, href = match.groups()
        if strong or alternate:
            parts.append('<strong>' + _e(strong or alternate) + '</strong>')
        elif code:
            parts.append('<code>' + _e(code) + '</code>')
        else:
            parts.append('<a href="' + _e(href) + '" target="_blank" rel="noopener noreferrer">'
                         + _e(label) + '</a>')
        cursor = match.end()
    parts.append(_e(text[cursor:]))
    return ''.join(parts)


def _legacy_markdown_html(text):
    """Render existing Markdown, not extracted source, without executable HTML.

    Keep the app's heading/list/inline/code subset and complete prose. Plain
    paragraphs retain their original line breaks in the existing pre-wrap field.
    """
    lines = re.sub(r'<br\s*/?\s*>', '\n', text.replace('\r\n', '\n').replace('\r', '\n'), flags=re.I).split('\n')
    blocks, paragraph, listing, code = [], [], None, None

    def flush():
        nonlocal listing
        if paragraph:
            blocks.append(_markdown_inline('\n'.join(paragraph)))
            paragraph.clear()
        if listing is not None:
            tag, start, items = listing
            attr = f' start="{start}"' if tag == 'ol' else ''
            blocks.append(f'<{tag}{attr}>' + ''.join('<li>' + _markdown_inline(item)
                + '</li>' for item in items) + f'</{tag}>')
            listing = None

    for line in lines:
        if re.match(r'^\s*```', line):
            flush()
            if code is None:
                code = []
            else:
                blocks.append('<pre>' + _e('\n'.join(code)) + '</pre>')
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            flush()
            blocks.append('')
            continue
        heading = re.match(r'^\s{0,3}#{1,6}[ \t]+(.+)$', line)
        if heading:
            flush()
            title = re.sub(r'[ \t]+#+[ \t]*$', '', heading[1]).rstrip()
            blocks.append('<strong>' + _markdown_inline(title) + '</strong>')
            continue
        bullet = re.match(r'^\s*([-+*]|[0-9]+[.)])\s+(.+)$', line)
        if bullet:
            ordered = bullet[1][0].isdigit()
            tag = 'ol' if ordered else 'ul'
            if listing is None or listing[0] != tag:
                flush()
                # Browser counters use signed 32-bit values. Never inject the
                # token itself into HTML or interpret dates as numbered lists.
                start = int(bullet[1][:-1]) if ordered and len(bullet[1]) <= 10 else 1
                listing = (tag, start, [])
            listing[2].append(bullet[2])
            continue
        if listing is not None and re.match(r'^\s{2,}\S', line):
            listing[2][-1] += '\n' + line.strip()
            continue
        if listing is not None:
            flush()
        if re.match(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$', line):
            flush()
            blocks.append('<hr>')
            continue
        paragraph.append(line)
    flush()
    if code is not None:
        blocks.append('<pre>' + _e('\n'.join(code)) + '</pre>')
    return '\n'.join(blocks)


def _check_gallery_size(byte_count: int) -> None:
    if byte_count > MAX_GALLERY_BYTES:
        raise GalleryTooLargeError('선택한 내용이 많아 HTML 파일을 만들지 않았습니다. '
                                   '내용을 자르지 않도록 소식을 나누어 선택한 뒤 다시 저장해주세요.')


def prepare_cache(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS news_media_cache (
        link TEXT PRIMARY KEY,
        poster_url TEXT NOT NULL DEFAULT '',
        checked_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'checked'
    )''')


def get_cached_poster(conn, link: str, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """신선한 포스터 캐시를 반환한다.

    빈 포스터 결과도 짧게 캐시해 이미지가 없는 페이지를 매 내보내기마다 다시 열지 않는다.
    """
    prepare_cache(conn)
    row = conn.execute(
        'SELECT poster_url, checked_at FROM news_media_cache WHERE link=?', (link,)
    ).fetchone()
    if not row:
        return {"hit": False, "poster_url": ""}
    try:
        checked = dt.datetime.fromisoformat(str(row["checked_at"]))
        if checked.tzinfo is not None:
            checked = checked.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return {"hit": False, "poster_url": ""}
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    poster_url = str(row["poster_url"] or "")
    ttl = POSITIVE_CACHE_DAYS if poster_url else NEGATIVE_CACHE_DAYS
    return {"hit": now - checked <= dt.timedelta(days=ttl), "poster_url": poster_url}


def store_poster(conn, link: str, poster_url: str, now: Optional[dt.datetime] = None) -> None:
    prepare_cache(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    value = now.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute('''INSERT INTO news_media_cache(link, poster_url, checked_at, status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(link) DO UPDATE SET
                        poster_url=excluded.poster_url,
                        checked_at=excluded.checked_at,
                        status=excluded.status''',
                 (link, poster_url or "", value, "found" if poster_url else "not_found"))
    conn.commit()


def _safe_http_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    return raw if parsed.scheme.lower() in ("http", "https") and parsed.netloc else ""


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _display_date(item: Dict[str, Any]) -> str:
    from services.news_timing import timing_review_note
    if timing_review_note(item):
        return '일정 확인 필요'
    return str(item.get("deadline") or item.get("event_end_date") or
               item.get("event_date") or item.get("date") or "일정 확인 필요")


def _copy_text(item: Dict[str, Any]) -> str:
    values = [
        str(item.get("title") or "제목 없음"),
        f"기관: {item.get('org') or item.get('site_name') or '-'}",
        f"일정: {_display_date(item)}",
    ]
    for label, detail in _details(item):
        values.extend(("", f"[{label}]", detail))
    review = _review_note(item)
    if review:
        values.extend(("", f"원문 확인 필요: {review}"))
    link = _safe_http_url(item.get("link"))
    if link:
        values.extend(("", f"원문: {link}"))
    return "\n".join(values)


def _review_note(item):
    from services.news_timing import timing_review_note
    timing_note = timing_review_note(item)
    ai = item.get('ai_data')
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except (ValueError, TypeError):
            ai = {}
    from services.news_display_projection import item_presentation
    display = item_presentation(item)
    notes = [value for value in (timing_note, display['source_note']) if value]
    if not isinstance(ai, dict):
        return ' '.join(notes)
    from services.page_coverage import input_note
    if note := input_note(ai):
        notes.append(note)
    for key, states in (('source_attachments', ('review_required',)),):
        state = ai.get(key)
        if isinstance(state, dict) and state.get('status') in states:
            notes.append(str(state.get('note') or '분석에서 빠진 내용이 있을 수 있습니다.')[:1000])
    return ' '.join(notes)


def _attachment_links(item):
    ai = item.get('ai_data')
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except (ValueError, TypeError):
            return []
    state = ai.get('source_attachments') if isinstance(ai, dict) else None
    files = state.get('files') if isinstance(state, dict) else None
    if not isinstance(files, list):
        return []
    links, seen = [], set()
    for entry in files[:30]:
        if not isinstance(entry, dict):
            continue
        url = _safe_http_url(entry.get('url'))
        if not url or url in seen or any(ord(ch) < 32 for ch in url):
            continue
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            continue
        seen.add(url)
        links.append((str(entry.get('filename') or '첨부파일')[:200], url))
    return links


def generate_html(items: Iterable[Dict[str, Any]]) -> str:
    """선택한 새소식을 외부 라이브러리 없는 단일 HTML 갤러리로 만든다."""
    rows = list(items)
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    cards = []
    card_bytes = 0
    for item in rows:
        title = _e(item.get("title") or "제목 없음")
        org = _e(item.get("org") or item.get("site_name") or "기관 미상")
        schedule = _e(_display_date(item))
        from services.news_timing import timing_review_note
        status = _e('기간 확인 필요' if timing_review_note(item) else item.get("status") or "일정 확인")
        detail = _detail_html(item)
        link = _safe_http_url(item.get("link"))
        poster = _safe_http_url(item.get("poster_url"))
        image = (
            f'<img src="{_e(poster)}" alt="{title} 홍보 이미지" loading="lazy" '
            'referrerpolicy="no-referrer" onerror="this.closest(\'.media\').classList.add(\'missing\');this.remove()">'
            if poster else ""
        )
        source = (f'<a class="source" href="{_e(link)}" target="_blank" '
                  'rel="noopener noreferrer">원문 보기</a>') if link else ""
        copy_value = _e(_copy_text(item))
        review = _review_note(item)
        warning = f'<p class="review">원문 확인 필요: {_e(review)}</p>' if review else ''
        attachment_links = _attachment_links(item)
        attachment_html = ('<details><summary>첨부파일 열기 (' + str(len(attachment_links)) + ')</summary>'
            '<p>원본 사이트에서 열립니다. 로그인이나 별도 다운로드가 필요할 수 있습니다.</p><ul>' +
            ''.join(f'<li><a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(name)}</a></li>'
                    for name, url in attachment_links) + '</ul></details>') if attachment_links else ''
        card = f'''
        <article class="card" data-copy="{copy_value}">
          <div class="media{' no-image' if not poster else ''}">{image}<div class="placeholder">복지F5 새소식</div></div>
          <div class="content">
            <div class="eyebrow"><span>{org}</span><span class="status">{status}</span></div>
            <h2>{title}</h2>
            <p class="date">{schedule}</p>
            {detail}
            {warning}
            {attachment_html}
            <div class="actions">{source}<button type="button" data-copy-action onclick="copyCard(this)">내용 복사</button></div>
          </div>
        </article>'''
        card_bytes += len(card.encode('utf-8'))
        _check_gallery_size(card_bytes)
        cards.append(card)

    document = f'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>복지F5 새소식 · {date_str}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f5f7fb;color:#172033;font-family:Pretendard,"Malgun Gothic",sans-serif}}
.wrap{{max-width:1280px;margin:auto;padding:40px 32px 56px}} header{{display:flex;align-items:end;justify-content:space-between;gap:24px;margin-bottom:26px}}
h1{{margin:0;font-size:30px;letter-spacing:-.04em}} header p{{margin:8px 0 0;color:#6b7280}} .all-copy{{border:1px solid #cbd5e1;background:white;border-radius:12px;padding:10px 15px;font-weight:700;cursor:pointer}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));gap:20px;align-items:start}}
.card{{width:100%;max-width:420px;min-width:0;overflow:hidden;border:1px solid #e5eaf2;border-radius:20px;background:white;box-shadow:0 8px 24px rgba(30,64,175,.06)}}
.media{{position:relative;aspect-ratio:16/10;background:#edf4ff;overflow:hidden}} .media img{{width:100%;height:100%;object-fit:contain;display:block}}
.media.no-image,.media.missing{{aspect-ratio:auto;height:64px}}
.placeholder{{display:none;position:absolute;inset:0;place-items:center;color:#5b7cba;font-weight:800;background:linear-gradient(135deg,#eef5ff,#f3f0ff)}} .media.no-image .placeholder,.media.missing .placeholder{{display:grid}}
.content{{padding:18px;overflow-wrap:anywhere}} .eyebrow{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px 12px;color:#557099;font-size:12px;font-weight:700}} .status{{color:#2563eb}}
h2{{font-size:18px;line-height:1.45;letter-spacing:-.02em;margin:10px 0 7px;overflow-wrap:anywhere}} .date{{margin:0;color:#64748b;font-size:13px}} .summary{{margin:14px 0 18px;color:#475569;font-size:14px;line-height:1.65;white-space:pre-wrap}}
.full-content{{margin:16px 0;border-top:1px solid #e5eaf2;border-bottom:1px solid #e5eaf2}} .full-content summary{{padding:12px 0;color:#334155;font-size:14px;font-weight:700;cursor:pointer}} .detail-count{{margin-left:8px;color:#64748b;font-size:12px;font-weight:400}} .full-content dl{{margin:0 0 14px}} .full-content dt{{margin:14px 0 6px;font-size:13px;font-weight:700;color:#334155}} .full-content dd{{margin:0;color:#475569;font-size:14px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}}
.actions{{display:flex;flex-wrap:wrap;align-items:center;gap:10px}} .actions a,.actions button{{min-height:38px;border-radius:10px;padding:9px 12px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer}}
.review{{padding:10px;border-radius:10px;background:#fffbeb;color:#92400e;font-size:13px;line-height:1.6;overflow-wrap:anywhere}}
.source{{background:#2563eb;color:white}} .actions button{{border:1px solid #dbe3ef;background:white;color:#334155}} button:disabled{{opacity:.5;cursor:default}} :focus-visible{{outline:3px solid #2563eb;outline-offset:3px}} .notice{{position:fixed;left:50%;bottom:28px;max-width:calc(100% - 36px);transform:translate(-50%,20px);opacity:0;background:#111827;color:white;padding:11px 16px;border-radius:12px;transition:.2s;pointer-events:none}} .notice.show{{opacity:1;transform:translate(-50%,0)}}
.manual-copy{{width:min(560px,calc(100% - 40px));max-height:85vh;overflow:auto;border:1px solid #dbe3ef;border-radius:20px;padding:24px;color:#172033;box-shadow:0 16px 48px #17203326}} .manual-copy::backdrop{{background:#17203366}} .manual-copy p{{font-size:14px;line-height:1.6;color:#475569}} .manual-copy textarea{{display:block;width:100%;height:200px;max-height:40vh;resize:vertical;border:1px solid #cbd5e1;border-radius:10px;padding:12px;font-family:inherit;font-size:14px;line-height:1.6;color:inherit}} .manual-copy form{{margin-top:16px;text-align:right}} .manual-copy button{{border:0;border-radius:10px;background:#2563eb;color:white;padding:10px 20px;font:inherit;cursor:pointer}}
@media(max-width:720px){{.wrap{{padding:28px 18px}}header{{align-items:start;flex-direction:column}}h1{{font-size:25px}}}}
@media print{{body{{background:white}}.wrap{{max-width:none;padding:0}}.grid{{display:block}}.card{{max-width:none;break-inside:auto;box-shadow:none;margin-bottom:20px;overflow:visible}}.content{{overflow:visible}}.media{{break-inside:avoid}}.actions,.all-copy,.notice,.manual-copy,.detail-count{{display:none}}}}
</style>
</head>
<body><main class="wrap"><header><div><h1>복지F5 새소식</h1><p>{date_str} 기준 · {len(rows)}건</p></div><button class="all-copy" type="button" data-copy-action {'disabled' if not rows else ''} onclick="copyAll()">전체 내용 복사</button></header>
<noscript><p>전체 내용을 인쇄하려면 먼저 각 소식의 상세 내용과 첨부파일을 펼쳐주세요.</p></noscript><section class="grid">{''.join(cards) if cards else '<p>내보낼 새소식이 없습니다.</p>'}</section></main><div id="notice" class="notice" role="status" aria-live="polite"></div>
<dialog id="manual-copy" class="manual-copy" aria-labelledby="manual-copy-title" aria-describedby="manual-copy-help"><h2 id="manual-copy-title">자동 복사가 안 됐습니다</h2><p id="manual-copy-help">아래 내용을 선택했습니다. Ctrl+C를 누르거나, 마우스 오른쪽 버튼을 눌러 복사해주세요.</p><textarea id="manual-copy-text" aria-label="직접 복사할 내용" readonly></textarea><form method="dialog"><button>닫기</button></form></dialog>
<script>
let printClosedDetails=[];
window.addEventListener('beforeprint',()=>{{
  for(const detail of document.querySelectorAll('.card details')){{
    if(!detail.open){{printClosedDetails.push(detail);detail.open=true}}
  }}
}});
window.addEventListener('afterprint',()=>{{
  for(const detail of printClosedDetails)detail.open=false;
  printClosedDetails=[];
}});
let copying=false,noticeTimer;
function notifyCopy(message){{const n=document.getElementById('notice');clearTimeout(noticeTimer);n.textContent=message;n.classList.add('show');noticeTimer=setTimeout(()=>n.classList.remove('show'),2500)}}
function fallbackCopy(text){{
  const area=document.createElement('textarea');
  area.value=text;area.readOnly=true;area.style.position='fixed';area.style.opacity='0';
  document.body.appendChild(area);
  try{{area.select();return document.execCommand('copy')===true}}catch(_e){{return false}}
  finally{{area.remove()}}
}}
function manualCopy(text){{
  const dialog=document.getElementById('manual-copy'),area=document.getElementById('manual-copy-text');
  document.getElementById('notice').classList.remove('show');
  area.value=text;dialog.showModal();area.focus();area.select();
}}
async function put(text){{
  if(copying)return;
  if(!text.trim()){{notifyCopy('복사할 내용이 없습니다.');return}}
  copying=true;
  const previous=document.activeElement;
  const buttons=[...document.querySelectorAll('[data-copy-action]')].map(button=>[button,button.disabled]);
  buttons.forEach(([button])=>{{button.disabled=true}});
  let copied=false;
  try{{
    try{{await navigator.clipboard.writeText(text);copied=true}}catch(_e){{copied=fallbackCopy(text)}}
  }}finally{{buttons.forEach(([button,disabled])=>{{button.disabled=disabled}});copying=false;if(previous && previous.isConnected)previous.focus({{preventScroll:true}})}}
  if(copied)notifyCopy('복사했습니다.');else manualCopy(text);
}}
function copyCard(button){{return put(button.closest('.card').dataset.copy)}}
function copyAll(){{return put([...document.querySelectorAll('.card')].map(card=>card.dataset.copy).join('\\n\\n──────────\\n\\n'))}}
</script></body></html>'''
    _check_gallery_size(len(document.encode('utf-8')))
    fd, filepath = create_temp_file(suffix=".html", prefix=f"WelfareF5_News_{date_str}_")
    os.close(fd)
    with open(filepath, "w", encoding="utf-8", newline="") as handle:
        handle.write(document)
    return filepath

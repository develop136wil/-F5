"""새소식 첨부파일 탐지와 로컬 텍스트 보강.

HTML 본문, 기본 수집, 프로필 수집이 서로 다른 첨부파일 규칙을 갖지 않도록 이 모듈의
짧은 직렬화 가능 메타데이터를 공유한다. 네트워크 파일은 크기와 리다이렉트를 제한하고,
PDF/HWPX처럼 로컬에서 읽을 수 있는 문서는 Gemini를 호출하기 전에 먼저 텍스트를 뽑는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import os
import re
import socket
import weakref
import zipfile
from email.message import Message
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp
from services.public_transport import public_get

from services.logger import setup_logger
from services.attachment_cache import AttachmentTextCache, url_key
from services.document_text import (DocumentText, extract_pdf_document,
                                    extract_hwpx_document, has_pdf_text_damage)
from services.local_document_process import extract_document_isolated


logger = setup_logger(__name__)
_LOCAL_LIMITS = weakref.WeakKeyDictionary()

SUPPORTED_KINDS = ("pdf", "hwpx", "hwp", "png", "jpg", "jpeg", "webp", "image")
IMAGE_KINDS = frozenset(("png", "jpg", "jpeg", "webp", "image"))
DOWNLOAD_TIMEOUT_SECONDS = 40
_TEXT_CACHE = AttachmentTextCache()
_EXT_RE = re.compile(r"\.((?:hwpx?)|pdf|png|jpe?g|webp)(?=$|[?#&;\s])", re.I)
_DOWNLOAD_URL_RE = re.compile(
    r"(?:download|filedown|file_down|filedownload|atchfile|attach(?:ment)?|attachno|filesn|fileid)",
    re.I,
)
_DOWNLOAD_LABEL_RE = re.compile(r"(?:첨부|붙임|다운로드|파일|서식|신청서|안내문|포스터)", re.I)


def _clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _kind_from_text(*values: Any) -> str:
    for value in values:
        decoded = unquote(str(value or ""))
        matches = list(_EXT_RE.finditer(decoded))
        if matches:
            kind = matches[-1].group(1).lower()
            return "jpg" if kind == "jpeg" else kind
    return ""


def discover_attachments(soup, base_url: str) -> List[Dict[str, str]]:
    """첨부 링크를 URL 확장자뿐 아니라 파일명·링크 문구·download 속성으로 찾는다."""
    found: List[Dict[str, str]] = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        # Global navigation downloads are not attachments of the current article.
        # Keep article-local headers/footers, which can legitimately contain files.
        if any(parent.name in ('nav', 'aside')
               or parent.get('role') == 'navigation'
               or (parent.name in ('header', 'footer')
                   and parent.find_parent(['article', 'main', 'section']) is None)
               for parent in anchor.parents):
            continue
        href = _clean_text(anchor.get("href"), 5000)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            absolute = urljoin(base_url, href)
            parsed = urlsplit(absolute)
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue
        absolute = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if absolute in seen:
            continue

        label = _clean_text(anchor.get_text(" ", strip=True))
        declared_name = _clean_text(
            anchor.get("download") or anchor.get("data-filename") or anchor.get("data-file-name")
        )
        title = _clean_text(anchor.get("title"))
        kind = _kind_from_text(declared_name, label, title, absolute)
        looks_downloadable = bool(
            kind
            or anchor.has_attr("download")
            or (_DOWNLOAD_URL_RE.search(absolute) and _DOWNLOAD_LABEL_RE.search(" ".join((label, title))))
        )
        if not looks_downloadable:
            continue

        filename = declared_name
        if not filename:
            for value in (label, title, unquote(parsed.path.rsplit("/", 1)[-1])):
                if _kind_from_text(value):
                    filename = _clean_text(value)
                    break
        found.append({
            "url": absolute,
            "filename": filename,
            "label": label or title,
            "kind": kind,
        })
        seen.add(absolute)
        if len(found) >= 30:
            break
    return found


def normalize_attachment_items(value: Any) -> List[Dict[str, str]]:
    """이전 ``list[str]`` 반환값과 새 메타데이터 반환값을 모두 안전하게 받는다."""
    result: List[Dict[str, str]] = []
    seen = set()
    if not isinstance(value, (list, tuple)):
        return result
    for raw in value:
        if isinstance(raw, str):
            item = {"url": raw, "filename": "", "label": "", "kind": _kind_from_text(raw)}
        elif isinstance(raw, dict):
            item = {
                "url": _clean_text(raw.get("url"), 5000),
                "filename": _clean_text(raw.get("filename")),
                "label": _clean_text(raw.get("label")),
                "kind": _clean_text(raw.get("kind"), 20).lower(),
            }
            if item["kind"] not in SUPPORTED_KINDS:
                item["kind"] = _kind_from_text(item["filename"], item["label"], item["url"])
        else:
            continue
        if not item["url"] or item["url"] in seen:
            continue
        result.append(item)
        seen.add(item["url"])
        if len(result) >= 30:
            break
    return result


async def validate_public_attachment_url(url: str) -> str:
    """첨부파일과 모든 리다이렉트 목적지가 공개 HTTP(S) 주소인지 확인한다."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError as exc:
        raise ValueError("잘못된 첨부파일 주소입니다.") from exc
    if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or len(url) > 10000):
        raise ValueError("공개 HTTP(S) 첨부파일 주소가 아닙니다.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("로컬 첨부파일 주소는 허용되지 않습니다.")

    def resolve() -> Iterable[str]:
        try:
            direct = ipaddress.ip_address(hostname)
            return (str(direct),)
        except ValueError:
            infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
            return tuple({info[4][0] for info in infos})

    try:
        addresses = await asyncio.to_thread(resolve)
    except (OSError, UnicodeError) as exc:
        raise ValueError("첨부파일 서버 주소를 확인할 수 없습니다.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("내부망 또는 예약된 첨부파일 주소는 허용되지 않습니다.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _filename_from_disposition(header: str) -> str:
    if not header:
        return ""
    try:
        message = Message()
        message["content-disposition"] = header
        return _clean_text(message.get_filename())
    except Exception:
        return ""


def _kind_from_content(content_type: str, data: bytes, *names: str) -> str:
    # 파일명이나 응답 MIME만 믿으면 로그인 HTML을 PDF로 AI에 보내게 된다.
    # 실제 바이트 시그니처를 우선 확인하고, 이름은 OLE/HWP 구분의 보조 근거로만 쓴다.
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if (data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            and (_kind_from_text(*names) == "hwp"
                 or content_type in ("application/haansofthwp", "application/x-hwp"))):
        return "hwp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names_lower = {name.lower() for name in archive.namelist()}
                if any(name.startswith("contents/section") and name.endswith(".xml") for name in names_lower):
                    return "hwpx"
        except (OSError, zipfile.BadZipFile):
            pass
    return ""


async def download_attachment(session: aiohttp.ClientSession, item: Dict[str, str], *,
                              max_bytes: Optional[int] = None, url_validator=None,
                              revalidation=None) -> Dict[str, Any]:
    """DNS 확인과 모든 리다이렉트·본문 수신을 합쳐 대기 시간을 제한한다."""
    async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
        return await _download_attachment(session, item, max_bytes=max_bytes,
                                          url_validator=url_validator, revalidation=revalidation)


async def _download_attachment(session: aiohttp.ClientSession, item: Dict[str, str], *,
                               max_bytes: Optional[int] = None, url_validator=None,
                               revalidation=None) -> Dict[str, Any]:
    """크기 제한과 수동 리다이렉트 검증을 거쳐 첨부파일 한 개를 내려받는다."""
    if max_bytes is None:
        try:
            max_bytes = int(os.getenv("NEWS_ATTACHMENT_MAX_BYTES", str(20 * 1024 * 1024)))
        except (TypeError, ValueError):
            max_bytes = 20 * 1024 * 1024
    max_bytes = max(1024 * 1024, min(max_bytes, 50 * 1024 * 1024))
    validator = url_validator or validate_public_attachment_url
    request_url = item["url"]
    for _ in range(6):
        request_url = await asyncio.wait_for(validator(request_url), timeout=10)
        request_headers = {}
        # Validators belong to the exact final representation, never to a new redirect
        # target. Cookie-based responses are not shared through this public-text cache.
        cookie_jar = getattr(session, 'cookie_jar', None)
        from yarl import URL
        has_cookies = bool(cookie_jar is not None and cookie_jar.filter_cookies(URL(request_url)))
        if (revalidation and revalidation.get('conditional_allowed') and not has_cookies
                and revalidation.get('final_key') == url_key(request_url)):
            for source, header in (('etag', 'If-None-Match'), ('last_modified', 'If-Modified-Since')):
                value = str(revalidation.get(source) or '')
                if value and len(value) <= 1000 and not re.search(r'[\x00-\x1f\x7f]', value):
                    request_headers[header] = value
        async with public_get(session, request_url, timeout=aiohttp.ClientTimeout(total=30),
                               allow_redirects=False, headers=request_headers) as response:
            cache_directives = str(response.headers.get('Cache-Control') or '').lower()
            no_store = (has_cookies or bool(response.headers.get('Set-Cookie'))
                        or bool(re.search(r'(?:^|,)\s*(?:no-store|private)\b', cache_directives)))
            vary = {value.strip().lower() for value in str(response.headers.get('Vary') or '').split(',') if value.strip()}
            no_store = no_store or bool(vary - {'accept-encoding'})
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("첨부파일 리다이렉트 주소가 비어 있습니다.")
                request_url = urljoin(request_url, location)
                continue
            if response.status == 304:
                changed_etag = (revalidation and revalidation.get('etag') and response.headers.get('ETag')
                                and revalidation['etag'] != response.headers['ETag'])
                if not request_headers or no_store or changed_etag:
                    # A bare 304 cannot prove our cached bytes are the current file.
                    revalidation = None
                    continue
                return {**revalidation, 'url': item['url'], 'final_url': str(response.url),
                        'not_modified': True, 'no_store': False,
                        'etag': response.headers.get('ETag') or revalidation.get('etag', ''),
                        'last_modified': response.headers.get('Last-Modified') or revalidation.get('last_modified', '')}
            if response.status != 200:
                raise ValueError(f"첨부파일 다운로드 HTTP {response.status}")
            declared_length = response.headers.get("Content-Length")
            if declared_length:
                try:
                    if int(declared_length) > max_bytes:
                        raise ValueError("첨부파일 용량이 제한을 초과합니다.")
                except ValueError as exc:
                    if "제한" in str(exc):
                        raise
            chunks = []
            total = 0
            while True:
                chunk = await response.content.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("첨부파일 용량이 제한을 초과합니다.")
            data = b"".join(chunks)
            content_type = str(response.headers.get("Content-Type") or "")
            filename = (_filename_from_disposition(response.headers.get("Content-Disposition") or "")
                        or item.get("filename") or "")
            kind = _kind_from_content(content_type, data, filename, item.get("label", ""), request_url)
            return {
                "url": item["url"], "final_url": str(response.url), "filename": filename,
                "label": item.get("label", ""), "kind": kind, "content_type": content_type,
                "data": data, "sha256": hashlib.sha256(data).hexdigest(),
                "etag": response.headers.get('ETag') or '',
                "last_modified": response.headers.get('Last-Modified') or '',
                "no_store": no_store,
            }
    raise ValueError("첨부파일 리다이렉트가 너무 많습니다.")


def extract_hwpx_text(data: bytes, *, max_chars: int = 30000) -> str:
    """호환용 텍스트 반환. 수집 경로는 범위 정보가 있는 document 반환값을 사용한다."""
    return extract_hwpx_document(data, max_chars=max_chars).text


def extract_pdf_text(data: bytes, *, max_chars: int = 30000, max_pages: int = 80) -> str:
    """호환용 텍스트 반환. 스캔 PDF는 빈 문자열을 반환한다."""
    return extract_pdf_document(data, max_chars=max_chars, max_pages=max_pages).text


def local_extraction_recipe():
    try:
        import pypdf
        version = pypdf.__version__
    except ImportError:
        version = 'unavailable'
    return f'news-text-v5:pypdf-{version}:hwp5-v1:korean-ocr-v5-v1:chars30000:pages80:ocrpages6'


def pdf_text_is_readable(text):
    """길이만 충분한 깨진 폰트 매핑을 정상 본문으로 오인하지 않는다."""
    return len(text) >= 120 and not has_pdf_text_damage(text)


async def _extract_downloaded(fetched):
    try:
        return await _extract_downloaded_inner(fetched)
    except asyncio.CancelledError:
        raise
    except Exception:
        return {**fetched, 'text': '', 'status': 'read_failed', 'extraction_cache_hit': False}


def local_ocr_status(extracted):
    """Reading text is not the same as certifying its facts or source completeness."""
    if not extracted.text.strip():
        return 'ocr_required'
    # Only the method marker is informational. Unknown warnings, omissions and
    # unread pages still need review even when the recognized text has high scores.
    if (set(extracted.reasons) <= {'ocr_verification_required'}
            and type(extracted.units_total) is int and extracted.units_total > 0
            and extracted.units_examined == extracted.units_total):
        return 'ocr_text_extracted'
    return 'partial_extracted'


async def _extract_downloaded_inner(fetched):
    kind = str(fetched.get('kind') or '')
    text = ''
    coverage = {}
    if kind == 'hwpx':
        extracted = await asyncio.to_thread(extract_hwpx_document, fetched['data'])
        text, coverage = extracted.text, extracted.coverage()
        status = 'partial_extracted' if extracted.reasons else ('text_extracted' if text else 'manual_review')
    elif kind == 'pdf':
        extracted = await extract_document_isolated(fetched['data'], 'pdf_text')
        text, coverage = extracted.text, extracted.coverage()
        readable = pdf_text_is_readable(text)
        limited = any(reason in ('page_limit', 'char_limit', 'stream_limit', 'page_read_error')
                      for reason in extracted.reasons)
        # A failed/limited first parser is not evidence of a scanned document.
        # Preserve its review reason without launching another costly parser.
        failed = any(reason in ('parser_unavailable', 'file_read_error', 'input_limit',
                                'extraction_timeout', 'worker_unavailable', 'worker_failed')
                     for reason in extracted.reasons)
        if failed:
            status = 'partial_extracted' if text else 'read_failed'
        elif extracted.reasons and (readable or limited):
            status = 'partial_extracted'
        else:
            status = 'text_extracted' if readable else 'ocr_required'
        if not readable and status != 'partial_extracted':
            text = ''  # Do not feed damaged glyphs to classification as reliable evidence.
    elif kind in IMAGE_KINDS:
        extracted = await extract_document_isolated(fetched['data'], 'image')
        text, coverage = extracted.text, extracted.coverage()
        status = local_ocr_status(extracted)
    elif kind == 'hwp':
        extracted = await extract_document_isolated(fetched['data'], 'hwp')
        text, coverage = extracted.text, extracted.coverage()
        status = 'partial_extracted' if extracted.reasons else ('text_extracted' if text else 'manual_review')
    else:
        status = 'unsupported'
    if kind == 'pdf' and status == 'ocr_required':
        extracted = await extract_document_isolated(fetched['data'], 'pdf_ocr')
        coverage = extracted.coverage()
        if extracted.text:
            text, status = extracted.text, local_ocr_status(extracted)
    return {**fetched, 'text': text, 'status': status, 'coverage': coverage, 'extraction_cache_hit': False}


async def load_local_attachment(session, item, *, cache=None, downloader=None):
    """매번 원격 변경을 확인한다. 캐시 오류/확인 실패를 오래된 본문으로 대체하지 않는다."""
    cache = _TEXT_CACHE if cache is None else cache
    downloader = download_attachment if downloader is None else downloader
    recipe = local_extraction_recipe()
    previous = await asyncio.to_thread(cache.get_resource, item['url'], recipe)
    fetched = await downloader(session, item, revalidation=previous)
    if fetched.get('not_modified'):
        result = {**fetched, 'extraction_cache_hit': True}
        await asyncio.to_thread(cache.store, item['url'], recipe, result,
                                full_checked_at=previous['full_checked_at'])
        return result
    # A new HTTP 200 invalidates the old URL binding even if the new body is corrupt
    # or unsupported. Content-addressed text can still be reused after a hash match.
    await asyncio.to_thread(cache.invalidate, item['url'])
    if fetched.get('no_store'):
        reusable = None
    else:
        reusable = await asyncio.to_thread(cache.get_content, fetched.get('sha256'), recipe)
    if reusable and reusable['kind'] == fetched.get('kind'):
        result = {**fetched, 'text': reusable['text'], 'status': reusable['status'],
                  'coverage': reusable.get('coverage', {}),
                  'extraction_cache_hit': True}
    else:
        result = await _extract_downloaded(fetched)
    if not fetched.get('no_store') and result['status'] in ('text_extracted', 'ocr_text_extracted'):
        await asyncio.to_thread(cache.store, item['url'], recipe, result)
    return result


async def enrich_page_locally(session: aiohttp.ClientSession, page: Dict[str, Any], *,
                              max_files: int = 2, downloader=None,
                              retain_pdf_for_ocr: bool = False) -> Dict[str, Any]:
    """한 번에 최대 두 게시물만 첨부 다운로드·로컬 추출을 진행한다."""
    if (not (page or {}).get('attachment_items') and not (page or {}).get('attachments')
            and not (page or {}).get('images')):
        return await _enrich_page_locally(session, page, max_files=max_files, downloader=downloader,
                                         retain_pdf_for_ocr=retain_pdf_for_ocr)
    loop = asyncio.get_running_loop()
    limiter_ref = _LOCAL_LIMITS.get(loop)
    limiter = limiter_ref() if limiter_ref is not None else None
    if limiter is None:
        limiter = asyncio.Semaphore(2)
        # Semaphore가 경합 후 loop를 참조하므로 값도 약한 참조로 보관한다.
        # 테스트/새 이벤트 루프가 종료된 뒤 전역 레지스트리가 이를 붙잡지 않는다.
        _LOCAL_LIMITS[loop] = weakref.ref(limiter)
    async with limiter:
        return await _enrich_page_locally(session, page, max_files=max_files, downloader=downloader,
                                         retain_pdf_for_ocr=retain_pdf_for_ocr)


async def _enrich_page_locally(session: aiohttp.ClientSession, page: Dict[str, Any], *,
                              max_files: int = 2, downloader=None,
                              retain_pdf_for_ocr: bool = False) -> Dict[str, Any]:
    """PDF/HWPX를 로컬 추출하고 나머지는 후속 OCR/검토 상태로 남긴다."""
    result = dict(page or {})
    items = normalize_attachment_items(result.get("attachment_items") or result.get("attachments"))
    # 본문이 거의 없는 이미지 공고도 미확인 근거로 보존한다. 긴 본문의 장식 이미지까지
    # 첨부 경고로 만들지 않는다. 선택된 이미지만 내려받아 로컬 OCR을 실행한다.
    if len(str(result.get("text") or "").strip()) < 500:
        images = result.get("images")
        if isinstance(images, list):
            known_urls = {item["url"] for item in items}
            for image_url in images[:2]:
                if len(items) >= 30:
                    break
                if not isinstance(image_url, str) or image_url in known_urls:
                    continue
                try:
                    parsed = urlsplit(image_url)
                except ValueError:
                    continue
                if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                    continue
                items.append({"url": image_url, "filename": "", "label": "본문 이미지",
                              "kind": "image"})
                known_urls.add(image_url)
    result["attachment_items"] = items
    result["attachments"] = [item["url"] for item in items]
    reports: List[Dict[str, str]] = []
    text_parts: List[str] = []
    fingerprints: List[str] = []
    if not items:
        result.update(attachment_text="", attachment_notices=[], attachment_reports=[],
                      attachment_fingerprint="")
        return result

    priority = {"hwpx": 0, "pdf": 1, "hwp": 2, "": 3, "png": 4, "jpg": 4, "jpeg": 4, "webp": 4, "image": 4}

    def rank(pair):
        index, entry = pair
        label = " ".join((entry.get("filename", ""), entry.get("label", "")))
        # 신청서가 먼저 달려 있어도 실제 안내문/공고문을 우선 읽는다.
        role = 2 if re.search(r"신청서|동의서|서식|양식", label) else (-1 if re.search(r"안내|공고|모집|사업|포스터", label) else 0)
        return role, priority.get(entry["kind"], 5), index

    ordered = sorted(enumerate(items), key=rank)
    selected = ordered[:max(1, min(max_files, 5))]
    selected_indexes = {index for index, _ in selected}
    for index, item in enumerate(items):
        display = item.get("filename") or item.get("label") or item["url"].rsplit("/", 1)[-1] or "첨부파일"
        if index not in selected_indexes:
            reports.append({"url": item["url"], "filename": display, "kind": item["kind"],
                            "status": "skipped_limit", "sha256": ""})

    for _index, item in selected:
        display = item.get("filename") or item.get("label") or item["url"].rsplit("/", 1)[-1] or "첨부파일"
        fetched = None
        try:
            if downloader is None:
                fetched = await load_local_attachment(session, item)
            else:
                # Injected downloaders are uncached, keeping fixture runs deterministic.
                fetched = await downloader(session, item)
                fetched = await _extract_downloaded(fetched)
            kind = str(fetched.get("kind") or "")
            display = fetched.get("filename") or display
            fingerprint = str(fetched.get("sha256") or "")
            if fingerprint:
                fingerprints.append(fingerprint)
            text = fetched['text']
            status = fetched['status']
            if retain_pdf_for_ocr and kind == 'pdf' and status == 'ocr_required' and '_ocr_payload' not in result:
                from services.ocr_payload import OcrPayload
                payload = OcrPayload.retain(item['url'], fetched.get('data'))
                if payload is not None:
                    result['_ocr_payload'] = payload
            if text:
                text_parts.append(f"--- [첨부파일: {display}] ---\n{text}")
            reports.append({"url": item["url"], "filename": display, "kind": kind,
                            "status": status, "sha256": fingerprint,
                            "coverage": fetched.get('coverage', {}),
                            "extraction_cache_hit": fetched.get('extraction_cache_hit', False)})
        except asyncio.CancelledError:
            payload = result.pop('_ocr_payload', None)
            if payload is not None:
                payload.close()
            raise
        except Exception as exc:
            logger.info(f"새소식 첨부파일 로컬 보강 실패 {item['url'][:120]}: {exc}")
            reports.append({"url": item["url"], "filename": display, "kind": item["kind"],
                            "status": "read_failed" if fetched is not None else "download_failed", "sha256": ""})

    notice_labels = {
        "ocr_required": "글자를 정확히 읽지 못해 이미지 분석 필요",
        "manual_review": "문서 원본 직접 확인 필요",
        "unsupported": "지원하지 않는 첨부 형식",
        "download_failed": "첨부파일 다운로드 실패",
        "read_failed": "첨부파일 내용을 읽지 못함",
        "skipped_limit": "첨부파일 개수 제한으로 미확인",
        "partial_extracted": "문서 일부만 읽음",
    }
    notices = [f"[첨부파일 상태: {coverage_description(report.get('coverage')) or notice_labels[report['status']]} - {report['filename']}]"
               for report in reports if report["status"] in notice_labels]
    result.update(
        attachment_text="\n\n".join(text_parts),
        attachment_notices=notices,
        attachment_reports=reports,
        attachment_fingerprint=hashlib.sha256("\n".join(sorted(fingerprints)).encode()).hexdigest()
        if fingerprints else "",
    )
    return result


def merge_page_text(page: Dict[str, Any], *, include_notices: bool = True) -> str:
    # AI 입력 길이 제한에 의해 뒤쪽이 잘려도 첨부의 실제 사업 내용이 메뉴/푸터보다
    # 먼저 전달되도록 첨부 텍스트를 앞에 둔다.
    parts = [str(page.get("attachment_text") or "").strip(), str(page.get("text") or "").strip()]
    if parts[0] and parts[1]:
        parts[1] = '--- [게시글 본문] ---\n' + parts[1]
    if include_notices:
        notices = page.get("attachment_notices")
        if isinstance(notices, list):
            parts.extend(str(value).strip() for value in notices if str(value).strip())
    return "\n\n".join(part for part in parts if part)


OCR_SELECTION_RECIPE = 'attachment-ocr-v2'


def select_ocr_images(reports, images, *, has_enough_text, pdf_selected=False):
    """Prefer explicitly pending attachments; never bypass file selection limits.

    PDF uses one slot. Combined PDF/image OCR attempts stay at most two.
    """
    known = {r.get('url'): r for r in reports if isinstance(r, dict)}
    candidates = [r.get('url') for r in reports if isinstance(r, dict)
                  and r.get('kind') in IMAGE_KINDS and r.get('status') == 'ocr_required']
    if not has_enough_text:
        candidates.extend(url for url in images if isinstance(url, str) and url not in known)
    selected = []
    for url in candidates:
        if not isinstance(url, str) or url in selected:
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in ('https', 'http') and parsed.netloc:
            selected.append(url)
    return selected[:1 if pdf_selected else 2]


def summarize_attachment_reports(reports: Any) -> Dict[str, Any]:
    """AI가 만든 판정과 구분되는 코드 기반 첨부 확인 상태를 저장/표시한다."""
    if not isinstance(reports, list) or not reports:
        return {"count": 0, "analyzed": 0, "cache_hits": 0, "status": "none", "note": "", "files": []}
    clean = []
    cache_hits = 0
    labels = {
        "text_extracted": "본문 읽음", "ocr_complete": "이미지 분석 완료",
        "ocr_text_extracted": "PC에서 이미지 글자 읽음",
        "ocr_required": "이미지 분석 필요", "ocr_failed": "이미지 분석 실패",
        "ai_transmission_held": "원본 파일 직접 확인 필요 — AI 전송 보류",
        "manual_review": "직접 확인 필요", "unsupported": "지원하지 않는 형식",
        "download_failed": "다운로드 실패", "skipped_limit": "개수 제한으로 미확인",
        "read_failed": "내용 읽기 실패",
        "partial_extracted": "일부만 읽음",
    }
    for report in reports[:30]:
        if not isinstance(report, dict):
            continue
        state = str(report.get("status") or "manual_review")
        if state in ('text_extracted', 'ocr_text_extracted') and report.get('extraction_cache_hit') is True:
            cache_hits += 1
        raw_url = report.get('url')
        safe_url = ''
        if isinstance(raw_url, str) and not any(ord(ch) < 32 for ch in raw_url):
            try:
                parsed = urlsplit(raw_url)
                if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password:
                    safe_url = raw_url
            except ValueError:
                pass
        clean.append({"url": safe_url, "filename": _clean_text(report.get("filename"), 200),
                      "kind": _clean_text(report.get("kind"), 20),
                      "status": state, "label": (labels[state] if state == 'ocr_text_extracted' else coverage_description(report.get('coverage')))
                      or labels.get(state, "직접 확인 필요")})
    read_states = ("text_extracted", "ocr_complete", "ocr_text_extracted")
    analyzed = sum(report["status"] in read_states for report in clean)
    incomplete = [report for report in clean if report["status"] not in read_states]
    note = "; ".join(f"{report['filename'] or '첨부파일'}: {report['label']}" for report in incomplete)[:1000]
    return {"count": len(clean), "analyzed": analyzed, "cache_hits": cache_hits,
            "status": "review_required" if incomplete else "complete", "note": note, "files": clean}


def coverage_description(coverage):
    if not isinstance(coverage, dict) or not isinstance(coverage.get('reasons'), list):
        return ''
    messages = []
    labels = {
        'page_limit': '페이지 수 제한으로 뒷부분은 읽지 못함',
        'char_limit': '내용 길이 제한으로 뒷부분은 읽지 못함',
        'stream_limit': '용량이 큰 페이지는 읽지 못함',
        'page_read_error': '읽지 못한 페이지가 있음',
        'unreadable_page': '글자가 깨진 페이지는 읽지 못함',
        'no_text_page': '이미지 등 글자를 읽지 못한 페이지가 있음',
        'section_limit': '용량이 큰 문서 구역은 읽지 못함',
        'section_read_error': '읽지 못한 문서 구역이 있음',
        'section_no_text': '이미지 등 글자를 읽지 못한 구역이 있음',
        'preview_only': '문서 전체가 아닌 미리보기 내용만 읽음',
        'parser_unavailable': '문서를 읽는 도구를 사용할 수 없음',
        'ocr_verification_required': '이미지에서 글자를 읽음 · 날짜·금액·조건은 원문 확인 필요',
        'ocr_unavailable': '로컬 이미지 인식 도구를 사용할 수 없음',
        'ocr_model_unavailable': '로컬 이미지 인식 모델을 사용할 수 없음',
        'ocr_models_unavailable': '로컬 이미지 인식 모델을 사용할 수 없음',
        'image_read_error': '이미지 파일을 안전하게 읽지 못함',
        'no_text_image': '이미지에서 읽을 수 있는 글자를 찾지 못함',
        'ocr_read_error': '이미지 글자 인식 중 오류가 발생함',
        'ocr_model_invalid': '로컬 이미지 인식 모델을 확인할 수 없음',
        'ocr_low_confidence': '이미지 글자의 인식 정확도가 낮아 원문 확인 필요',
        'ocr_omitted_text': '정확히 읽지 못한 이미지 글자는 제외됨',
        'ocr_no_text': '이미지에서 읽을 수 있는 글자를 찾지 못함',
        'image_size_limit': '이미지 크기 제한으로 읽지 못함',
        'extraction_timeout': '로컬 문서 읽기 제한 시간을 초과함',
        'worker_unavailable': '로컬 문서 읽기 도구를 시작하지 못함',
        'worker_failed': '로컬 문서 읽기가 중단됨',
        'input_limit': '파일 용량 제한으로 읽지 못함',
        'file_limit': '파일 용량 제한으로 읽지 못함',
        'file_read_error': '손상되었거나 안전하게 읽을 수 없는 문서',
        'encrypted_document': '암호가 설정된 문서는 직접 확인 필요',
        'distributed_document': '배포용 한글 문서는 직접 확인 필요',
        'protected_document': '보호된 한글 문서는 직접 확인 필요',
        'unsupported_version': '지원하지 않는 한글 문서 버전',
        'embedded_content_review': '그림·수식·삽입 문서 등은 원문 확인 필요',
        'layout_review': '표의 행·열 관계와 배치는 원문 확인 필요',
        'document_structure_review': '문서 구성 일부는 원문 확인 필요',
        'section_missing': '빠진 문서 구역이 있어 원문 확인 필요',
        'record_limit': '문서 구성 개수 제한으로 일부만 읽음',
        'total_size_limit': '압축 해제 용량 제한으로 일부만 읽음',
        'container_limit': '문서 구성 개수 제한으로 읽지 못함',
        'unsupported_control': '지원하지 않는 문서 제어 내용은 원문 확인 필요',
        'unsupported_feature': '지원하지 않는 문서 기능은 원문 확인 필요',
        'preview_read_error': '문서 미리보기를 읽지 못함',
    }
    for reason in coverage['reasons']:
        if not isinstance(reason, str) or reason not in labels:
            continue
        message = labels[reason]
        examined, total = coverage.get('units_examined'), coverage.get('units_total')
        if reason == 'page_limit' and type(examined) is int and type(total) is int and 0 < examined < total:
            message = f'전체 {total}쪽 중 앞 {examined}쪽까지만 읽음'
        limit = coverage.get('max_chars')
        if reason == 'char_limit' and type(limit) is int and limit > 0:
            message = f'내용이 길어 {limit:,}자까지만 읽음'
        if message not in messages:
            messages.append(message)
    return ' / '.join(messages)[:400]

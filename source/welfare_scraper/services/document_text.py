"""Bounded local document extraction with explicit incomplete-coverage metadata."""
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree


@dataclass
class DocumentText:
    text: str = ''
    reasons: list[str] = field(default_factory=list)
    units_total: int = 0
    units_examined: int = 0
    max_chars: int = 30000

    def mark(self, reason):
        if reason not in self.reasons:
            self.reasons.append(reason)

    def coverage(self):
        return {'reasons': list(self.reasons), 'units_total': self.units_total,
                'units_examined': self.units_examined, 'max_chars': self.max_chars}


class TextBudget:
    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError('Text limit must be positive')
        self.limit, self.size, self.parts = limit, 0, []

    def append(self, value):
        value = re.sub(r'\s+', ' ', value or '').strip()
        if not value:
            return True
        remaining = self.limit - self.size - bool(self.parts)
        if remaining <= 0:
            return False
        piece = value[:remaining]
        self.size += len(piece) + bool(self.parts)
        self.parts.append(piece)
        return len(value) <= remaining

    def text(self):
        return ' '.join(self.parts)


def has_pdf_text_damage(text):
    if not text:
        return False
    damaged = sum((unicodedata.category(char) in ('Cc', 'Co') and char not in '\n\r\t')
                  or char == '\ufffd' for char in text)
    return damaged >= 3 or damaged / len(text) >= 0.01 or len(re.findall(r'\(cid:\d+\)', text)) >= 3


def extract_pdf_document(data, *, max_chars=30000, max_pages=80):
    result, budget = DocumentText(max_chars=max_chars), TextBudget(max_chars)
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError('Page limit must be positive')
    try:
        from pypdf import PdfReader, filters
    except ImportError:
        result.mark('parser_unavailable')
        return result
    try:
        for setting in ('MAX_DECLARED_STREAM_LENGTH', 'MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH',
                        'LZW_MAX_OUTPUT_LENGTH', 'RUN_LENGTH_MAX_OUTPUT_LENGTH', 'ZLIB_MAX_OUTPUT_LENGTH'):
            if hasattr(filters, setting):
                setattr(filters, setting, min(getattr(filters, setting), 8 * 1024 * 1024))
        document = PdfReader(io.BytesIO(data), strict=False)
        if document.is_encrypted:
            raise ValueError('암호가 설정된 PDF는 직접 확인해야 합니다.')
        result.units_total = len(document.pages)
        if result.units_total > max_pages:
            result.mark('page_limit')
        for index in range(min(result.units_total, max_pages)):
            result.units_examined += 1
            try:
                page = document.pages[index]
                stream = page.get_contents()
                stream_data = stream.get_data() if stream is not None else b''
                if len(stream_data) > 8 * 1024 * 1024:
                    result.mark('stream_limit')
                    continue
                value = page.extract_text() or ''
                if has_pdf_text_damage(value):
                    result.mark('unreadable_page')
                    continue
                if not value.strip() and stream_data.strip():
                    result.mark('no_text_page')
                    continue
            except Exception:
                # One damaged page must not hide the readable pages or claim completion.
                result.mark('page_read_error')
                continue
            if not budget.append(value):
                result.mark('char_limit')
                break
    except Exception as exc:
        raise ValueError('손상되었거나 안전하게 읽을 수 없는 PDF입니다.') from exc
    result.text = budget.text()
    return result


def extract_hwpx_document(data, *, max_chars=30000):
    result, budget = DocumentText(max_chars=max_chars), TextBudget(max_chars)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2000 or sum(info.file_size for info in infos) > 50 * 1024 * 1024:
                raise ValueError('HWPX 압축 해제 용량이 제한을 초과합니다.')
            sections = sorted((info for info in infos
                               if re.fullmatch(r'Contents/section\d+\.xml', info.filename, re.I)),
                              key=lambda info: int(re.search(r'(\d+)', info.filename).group(1)))
            result.units_total = len(sections)
            for info in sections:
                result.units_examined += 1
                # Check before decompression as well as after reading the entry.
                if info.file_size > 10 * 1024 * 1024:
                    result.mark('section_limit')
                    continue
                try:
                    xml = archive.read(info)
                    if len(xml) > 10 * 1024 * 1024:
                        result.mark('section_limit')
                        continue
                    # Also reject declarations in UTF-16/32 XML, before parsing entities.
                    declarations = xml.replace(b'\x00', b'').upper()
                    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
                        result.mark('section_read_error')
                        continue
                    root = ElementTree.fromstring(xml)
                except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
                    result.mark('section_read_error')
                    continue
                before = budget.size
                has_picture = False
                for node in root.iter():
                    tag = node.tag.rsplit('}', 1)[-1].lower()
                    has_picture = has_picture or tag in ('pic', 'ole', 'equation')
                    if tag == 't' and node.text and not budget.append(node.text):
                        result.mark('char_limit')
                        break
                if 'char_limit' in result.reasons:
                    break
                if before == budget.size and has_picture:
                    result.mark('section_no_text')
                elif has_picture:
                    result.mark('embedded_content_review')
            if not budget.text():
                for name in ('Preview/PrvText.txt', 'preview/prvtext.txt'):
                    try:
                        if archive.getinfo(name).file_size > 10 * 1024 * 1024:
                            result.mark('section_limit')
                            break
                        preview = archive.read(name).decode('utf-8', errors='replace')
                    except KeyError:
                        continue
                    if preview.strip():
                        result.mark('preview_only')
                        if not budget.append(preview):
                            result.mark('char_limit')
                    break
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError('손상되었거나 지원하지 않는 HWPX 파일입니다.') from exc
    result.text = budget.text()
    return result

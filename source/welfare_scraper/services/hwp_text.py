"""Bounded, read-only HWP 5 paragraph extraction (not a document renderer).

Format reference: Hancom HWP Document File Formats 5.0, revision 1.3,
sections 3.2.1, 3.2.3, 3.2.6, 4.1 and 4.3.2:
https://cdn.hancom.com/link/docs/한글문서파일형식_5.0_revision1.3.pdf

Only FileHeader, DocInfo, BodyText/SectionN and optional PrvText are read.
Embedded objects, scripts, linked files and macros are never opened/executed.
Callers should additionally use a time-limited subprocess for untrusted files.
"""
import io
import re
import struct
import zlib

from services.document_text import DocumentText, TextBudget


_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_HWP_SIGNATURE = b'HWP Document File'.ljust(32, b'\0')
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_STREAM_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_MAX_RECORDS = 100000
_MAX_ENTRIES = 2000
_MAX_SECTIONS = 128
_MULTI_UNIT_CONTROLS = frozenset(range(1, 10)) | {11, 12} | frozenset(range(14, 24))
_RESERVED_CONTROLS = {1, 5, 6, 7, 12, 14, 19, 20, 25, 26, 27, 28, 29}
_EMBEDDED_TAGS = {16 + offset for offset in (68, 69, 72, 74, 75, 79, 82, 99)}


class _ReadError(ValueError):
    pass


def _stream(ole, path, *, limit=_MAX_STREAM_BYTES):
    declared = ole.get_size(path)
    if type(declared) is not int or not 0 <= declared <= limit:
        raise _ReadError('stream_limit')
    with ole.openstream(path) as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise _ReadError('stream_limit')
    if len(value) != declared:
        raise _ReadError('section_read_error')
    return value


def _unpack_section(data, compressed, remaining):
    limit = min(_MAX_STREAM_BYTES, remaining)
    if limit <= 0:
        raise _ReadError('total_size_limit')
    if not compressed:
        if len(data) > limit:
            raise _ReadError('stream_limit' if limit == _MAX_STREAM_BYTES else 'total_size_limit')
        return data
    decoder = zlib.decompressobj(-15)
    try:
        value = decoder.decompress(data, limit + 1)
    except zlib.error as exc:
        raise _ReadError('section_read_error') from exc
    if len(value) > limit or decoder.unconsumed_tail:
        raise _ReadError('stream_limit' if limit == _MAX_STREAM_BYTES else 'total_size_limit')
    # Do not use unbounded flush(). HWP writers may append the gzip-style
    # CRC32/ISIZE trailer to raw DEFLATE (observed in public HWP 5.1.1 files).
    # Accept only its exact, verified eight bytes, never arbitrary trailing
    # data or concatenated streams whose prefix happens to contain text.
    trailer = decoder.unused_data
    if not decoder.eof or (trailer and trailer != struct.pack('<II', zlib.crc32(value), len(value))):
        raise _ReadError('section_read_error')
    return value


def _records(data, counter):
    offset = 0
    while offset < len(data):
        if counter[0] >= _MAX_RECORDS:
            raise _ReadError('record_limit')
        if len(data) - offset < 4:
            raise _ReadError('section_read_error')
        header = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        tag, level, size = header & 0x3ff, (header >> 10) & 0x3ff, header >> 20
        if size == 0xfff:
            if len(data) - offset < 4:
                raise _ReadError('section_read_error')
            size = struct.unpack_from('<I', data, offset)[0]
            offset += 4
        if size > _MAX_RECORD_BYTES:
            raise _ReadError('record_limit')
        if size > len(data) - offset:
            raise _ReadError('section_read_error')
        counter[0] += 1
        yield tag, level, memoryview(data)[offset:offset + size]
        offset += size


def _paragraph(payload, result):
    if len(payload) % 2:
        raise _ReadError('section_read_error')
    output = bytearray()
    offset = 0
    while offset < len(payload):
        code = struct.unpack_from('<H', payload, offset)[0]
        if code >= 32:
            output.extend(payload[offset:offset + 2])
            offset += 2
            continue
        if code in _MULTI_UNIT_CONTROLS:
            # Both inline and extended controls occupy EIGHT UTF-16 units,
            # including their start/end markers, not eight bytes.
            if len(payload) - offset < 16 or struct.unpack_from('<H', payload, offset + 14)[0] != code:
                raise _ReadError('section_read_error')
            offset += 16
        else:
            offset += 2
        if code in _RESERVED_CONTROLS:
            result.mark('unsupported_control')
        if code in (11, 15, 16, 17, 23):
            result.mark('layout_review')
        if code == 24:
            output.extend(b'-\0')
        elif code != 0:
            output.extend(b' \0')
    try:
        return output.decode('utf-16le', errors='strict')
    except UnicodeDecodeError as exc:
        raise _ReadError('section_read_error') from exc


def _declared_sections(ole, compressed, result):
    if not ole.exists('DocInfo'):
        result.mark('document_structure_review')
        return None
    try:
        data = _unpack_section(_stream(ole, 'DocInfo'), compressed, _MAX_STREAM_BYTES)
        tag, level, payload = next(_records(data, [0]))
        if tag != 16 or level != 0 or len(payload) < 26:
            raise _ReadError('section_read_error')
        return struct.unpack_from('<H', payload)[0]
    except (OSError, ValueError, StopIteration):
        result.mark('document_structure_review')
        return None


def extract_hwp_document(data: bytes | bytearray, *, max_chars: int = 30000) -> DocumentText:
    """Return readable paragraph text plus explicit incomplete-coverage reasons.

    No password/distribution/DRM bypass is attempted. A preview is never passed
    off as the complete document, and flattened tables retain a review marker.
    """
    result, budget = DocumentText(max_chars=max_chars), TextBudget(max_chars)
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError('HWP input must be bytes')
    if len(data) > _MAX_FILE_BYTES:
        result.mark('file_limit')
        return result
    if len(data) < 512 or data[:8] != _OLE_MAGIC:
        result.mark('file_read_error')
        return result
    try:
        import olefile
    except ImportError:
        result.mark('parser_unavailable')
        return result
    try:
        with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT,
                               write_mode=False) as ole:
            # Bounds before reading streams, including the shared mini-stream.
            if len(ole.direntries) > _MAX_ENTRIES or getattr(ole.root, 'size', 0) > _MAX_FILE_BYTES:
                result.mark('container_limit')
                return result
            paths = ole.listdir(streams=True, storages=False)
            if len(paths) > _MAX_ENTRIES:
                result.mark('container_limit')
                return result
            header = _stream(ole, 'FileHeader', limit=256)
            if len(header) != 256 or header[:32] != _HWP_SIGNATURE:
                result.mark('file_read_error')
                return result
            version, flags = struct.unpack_from('<II', header, 32)
            if version >> 24 != 5:
                result.mark('unsupported_version')
                return result
            # EncryptVersion is an algorithm/version field, not an encrypted
            # state flag: ordinary unencrypted HWP 5 files also store 4 here.
            # The password/certificate encryption attribute bits are decisive.
            if flags & ((1 << 1) | (1 << 8)):
                result.mark('encrypted_document')
                return result
            if flags & (1 << 2):
                result.mark('distributed_document')
                return result
            if flags & ((1 << 4) | (1 << 10) | (1 << 13)):
                result.mark('protected_document')
                return result
            if flags >> 18:
                result.mark('unsupported_feature')
                return result
            if flags & (1 << 14):
                result.mark('layout_review')
            if any(path[0] == 'BinData' for path in paths):
                result.mark('embedded_content_review')
            compressed = bool(flags & 1)
            declared = _declared_sections(ole, compressed, result)
            sections = sorted((path for path in paths if len(path) == 2 and path[0] == 'BodyText'
                               and re.fullmatch(r'Section(?:0|[1-9][0-9]{0,5})', path[1])),
                              key=lambda path: int(path[1][7:]))
            result.units_total = max(len(sections), declared or 0)
            numbers = [int(path[1][7:]) for path in sections]
            if numbers != list(range(len(sections))) or (declared is not None and declared != len(sections)):
                result.mark('section_missing')
            if len(sections) > _MAX_SECTIONS:
                result.mark('section_limit')
            total_bytes, counter = 0, [0]
            for path in sections[:_MAX_SECTIONS]:
                result.units_examined += 1
                before = budget.size
                try:
                    section = _unpack_section(_stream(ole, path), compressed, _MAX_TOTAL_BYTES - total_bytes)
                    total_bytes += len(section)
                    for tag, level, payload in _records(section, counter):
                        if tag in _EMBEDDED_TAGS:
                            result.mark('embedded_content_review')
                        elif tag == 16 + 61:
                            result.mark('layout_review')
                        elif tag == 16 + 51:
                            if level > 1:
                                result.mark('layout_review')
                            value = _paragraph(payload, result)
                            if not budget.append(value):
                                result.mark('char_limit')
                                break
                    if before == budget.size and section:
                        result.mark('section_no_text')
                except _ReadError as exc:
                    result.mark(str(exc))
                except (OSError, ValueError, TypeError, IndexError, RecursionError):
                    result.mark('section_read_error')
                if any(reason in result.reasons for reason in ('char_limit', 'record_limit', 'total_size_limit')):
                    break
            if not budget.text() and ole.exists('PrvText'):
                try:
                    preview = _stream(ole, 'PrvText').decode('utf-16le', errors='strict').strip('\0')
                    if preview.strip():
                        result.mark('preview_only')
                        if not budget.append(preview):
                            result.mark('char_limit')
                except (OSError, ValueError, TypeError, IndexError, RecursionError):
                    result.mark('preview_read_error')
            if not sections and not budget.text():
                result.mark('section_no_text')
    except _ReadError as exc:
        result.mark(str(exc))
    except (OSError, ValueError, TypeError, IndexError, KeyError, RecursionError):
        result.mark('file_read_error')
    result.text = budget.text()
    return result

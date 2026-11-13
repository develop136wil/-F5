"""Synthetic HWP fixtures; no external document, application or network calls."""
import io
import struct
import sys
import types
import unittest
import zlib
from unittest.mock import patch

from services import hwp_text as hwp


def record(tag, payload=b'', level=1, *, extended=False):
    size = len(payload)
    header = tag | (level << 10) | ((0xfff if extended or size >= 0xfff else size) << 20)
    return struct.pack('<I', header) + (struct.pack('<I', size) if extended or size >= 0xfff else b'') + payload


def paragraph(text, level=1):
    return record(67, (text + '\r').encode('utf-16le'), level)


def deflate(value):
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(value) + compressor.flush()


def header(flags=0, version=0x05000300):
    data = bytearray(256)
    data[:32] = hwp._HWP_SIGNATURE
    struct.pack_into('<II', data, 32, version, flags)
    return bytes(data)


class FakeOle:
    def __init__(self, streams):
        self.streams = {tuple(key.split('/')): value for key, value in streams.items()}
        self.direntries = [None] * (len(streams) + 1)
        self.root = types.SimpleNamespace(size=0)
        self.reads = []
        self.sizes = {}

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def listdir(self, **kwargs): return list(self.streams)
    def exists(self, path): return tuple(path.split('/')) in self.streams
    def get_size(self, path):
        path = tuple(path.split('/')) if isinstance(path, str) else tuple(path)
        return self.sizes.get(path, len(self.streams[path]))
    def openstream(self, path):
        path = tuple(path.split('/')) if isinstance(path, str) else tuple(path)
        self.reads.append(path)
        value = self.streams[path]
        if isinstance(value, Exception): raise value
        return io.BytesIO(value)


def streams(sections=None, *, compressed=False, flags=0):
    sections = sections if sections is not None else [paragraph('복지 지원 안내')]
    encode = deflate if compressed else lambda value: value
    return {'FileHeader': header(flags | int(compressed)),
            'DocInfo': encode(record(16, struct.pack('<H', len(sections)) + b'\0' * 24, 0)),
            **{f'BodyText/Section{i}': encode(value) for i, value in enumerate(sections)}}


def synthetic_compound(values):
    """Minimal CFB v3 writer for tiny, generated mini-stream fixtures only."""
    free, end, fat_sector = 0xffffffff, 0xfffffffe, 0xfffffffd
    nodes = [{'name': 'Root Entry', 'kind': 5, 'parent': None}]
    storage_ids = {(): 0}
    for name in values:
        parts = tuple(name.split('/'))
        for length in range(1, len(parts)):
            prefix = parts[:length]
            if prefix not in storage_ids:
                storage_ids[prefix] = len(nodes)
                nodes.append(dict(name=prefix[-1], kind=1, parent=storage_ids[prefix[:-1]]))
        nodes.append(dict(name=parts[-1], kind=2, parent=storage_ids[parts[:-1]], data=values[name]))
    for node in nodes:
        node.update(left=free, right=free, child=free, start=end, size=0)
    def siblings(ids):
        if not ids: return free
        mid = len(ids) // 2
        current = ids[mid]
        nodes[current]['left'], nodes[current]['right'] = siblings(ids[:mid]), siblings(ids[mid + 1:])
        return current
    for index, node in enumerate(nodes):
        children = [i for i, value in enumerate(nodes) if value['parent'] == index]
        children.sort(key=lambda i: (len(nodes[i]['name']), nodes[i]['name'].upper()))
        node['child'] = siblings(children)
    mini, minifat = bytearray(), [free] * 128
    for node in nodes:
        if node['kind'] != 2: continue
        content = node['data']
        count = (len(content) + 63) // 64
        if not count: continue
        start = len(mini) // 64
        assert len(content) < 4096 and start + count <= 128
        node['start'], node['size'] = start, len(content)
        for index in range(start, start + count):
            minifat[index] = index + 1 if index < start + count - 1 else end
        mini.extend(content.ljust(count * 64, b'\0'))
    dir_count = (len(nodes) + 3) // 4
    mini_sector, fat_index, root_start = dir_count, dir_count + 1, dir_count + 2
    root_count = (len(mini) + 511) // 512
    nodes[0]['start'], nodes[0]['size'] = root_start, len(mini)
    directory = bytearray(dir_count * 512)
    for index, node in enumerate(nodes):
        base = index * 128
        name = (node['name'] + '\0').encode('utf-16le')
        directory[base:base + len(name)] = name
        struct.pack_into('<HBBIII', directory, base + 64, len(name), node['kind'], 1,
                         node['left'], node['right'], node['child'])
        struct.pack_into('<IQ', directory, base + 116, node['start'], node['size'])
    fat = [free] * 128
    for index in range(dir_count):
        fat[index] = index + 1 if index < dir_count - 1 else end
    fat[mini_sector], fat[fat_index] = end, fat_sector
    for index in range(root_start, root_start + root_count):
        fat[index] = index + 1 if index < root_start + root_count - 1 else end
    container = bytearray(512)
    container[:8] = hwp._OLE_MAGIC
    struct.pack_into('<HHHHH', container, 24, 0x3e, 3, 0xfffe, 9, 6)
    struct.pack_into('<IIIIIIIII', container, 40, 0, 1, 0, 0, 4096, mini_sector, 1, end, 0)
    struct.pack_into('<109I', container, 76, fat_index, *([free] * 108))
    return (bytes(container) + directory + struct.pack('<128I', *minifat)
            + struct.pack('<128I', *fat) + mini.ljust(root_count * 512, b'\0'))


class TestHwpText(unittest.TestCase):
    def extract(self, values, **kwargs):
        self.ole = FakeOle(values)
        module = types.SimpleNamespace(DEFECT_INCORRECT=30,
            OleFileIO=lambda *args, **settings: self.ole)
        with patch.dict(sys.modules, {'olefile': module}):
            return hwp.extract_hwp_document(hwp._OLE_MAGIC + b'\0' * 504, **kwargs)

    def test_compressed_and_uncompressed_paragraphs_and_natural_section_order(self):
        for compressed in (False, True):
            result = self.extract(streams([paragraph(f'지원 {i}') for i in range(12)], compressed=compressed))
            self.assertEqual(result.text, ' '.join(f'지원 {i}' for i in range(12)))
            self.assertEqual(result.reasons, [])
            self.assertEqual((result.units_total, result.units_examined), (12, 12))

    def test_inline_extended_controls_skip_eight_utf16_units_not_eight_bytes(self):
        def control(code):
            return struct.pack('<H', code) + '거짓내용금지'.encode('utf-16le') + struct.pack('<H', code)
        payload = ('신청'.encode('utf-16le') + control(2) + '대상'.encode('utf-16le')
                   + control(9) + '아동'.encode('utf-16le') + control(11) + '보호자\r'.encode('utf-16le'))
        result = self.extract(streams([record(67, payload)]))
        self.assertEqual(result.text, '신청 대상 아동 보호자')
        self.assertNotIn('거짓', result.text)
        self.assertIn('layout_review', result.reasons)

    def test_single_unit_controls_and_surrogate_pair(self):
        result = self.extract(streams([paragraph('지원\x1e아동\x1f가족\x18안내\n다음 😀')]))
        self.assertEqual(result.text, '지원 아동 가족-안내 다음 😀')
        self.assertEqual(result.reasons, [])

    def test_table_nested_paragraphs_are_read_but_layout_is_not_certified(self):
        result = self.extract(streams([record(77, b'layout') + paragraph('지원 대상', 3) + paragraph('노인', 3)]))
        self.assertEqual(result.text, '지원 대상 노인')
        self.assertEqual(result.reasons, ['layout_review'])

    def test_embedded_objects_are_reviewed_and_never_opened(self):
        values = streams([paragraph('본문') + record(85, b'picture')])
        values.update({'BinData/BIN0001.jpg': b'not opened',
                       'Scripts/DefaultJScript': b'not executed',
                       'DocOptions/_LinkDoc': b'not followed'})
        result = self.extract(values)
        self.assertEqual(result.text, '본문')
        self.assertIn('embedded_content_review', result.reasons)
        self.assertTrue(all(path[0] not in ('BinData', 'Scripts', 'DocOptions') for path in self.ole.reads))

    def test_secure_variants_do_not_return_even_plain_preview(self):
        cases = [(1 << 1, 'encrypted_document'), (1 << 8, 'encrypted_document'),
                 (1 << 2, 'distributed_document'), (1 << 4, 'protected_document'),
                 (1 << 10, 'protected_document'), (1 << 13, 'protected_document'),
                 (1 << 18, 'unsupported_feature')]
        for flag, reason in cases:
            values = streams(flags=flag)
            values['PrvText'] = '보호된 미리보기'.encode('utf-16le')
            result = self.extract(values)
            self.assertEqual((result.text, result.reasons), ('', [reason]))
            self.assertEqual(self.ole.reads, [('FileHeader',)])

    def test_signature_version_header_size_and_encryption_version(self):
        for bad_header, reason in ((b'wrong'.ljust(256, b'\0'), 'file_read_error'),
                                   (header(version=0x04000000), 'unsupported_version'),
                                   (header()[:255], 'file_read_error'),
                                   (header() + b'x', 'stream_limit')):
            result = self.extract({**streams(), 'FileHeader': bad_header})
            self.assertFalse(result.text)
            self.assertIn(reason, result.reasons)
        unencrypted = bytearray(header())
        struct.pack_into('<I', unencrypted, 44, 4)
        result = self.extract({**streams(), 'FileHeader': bytes(unencrypted)})
        self.assertEqual(result.text, '복지 지원 안내')
        self.assertEqual(result.reasons, [])
        for flag in (1 << 1, 1 << 8):
            struct.pack_into('<I', unencrypted, 36, flag)
            result = self.extract({**streams(), 'FileHeader': bytes(unencrypted)})
            self.assertEqual((result.text, result.reasons), ('', ['encrypted_document']))

    def test_missing_body_preview_is_explicitly_partial(self):
        values = streams([])
        values['PrvText'] = '미리보기 일부\0'.encode('utf-16le')
        result = self.extract(values)
        self.assertEqual(result.text, '미리보기 일부')
        self.assertIn('preview_only', result.reasons)
        self.assertEqual(result.units_examined, 0)

    def test_corrupt_preview_is_not_returned_as_readable_text(self):
        result = self.extract({**streams([]), 'PrvText': b'\xff'})
        self.assertEqual(result.text, '')
        self.assertIn('preview_read_error', result.reasons)

    def test_extended_record_size_and_character_limit(self):
        long_text = '가나다 ' * 2000
        result = self.extract(streams([record(67, long_text.encode('utf-16le'), extended=True)]), max_chars=17)
        self.assertLessEqual(len(result.text), 17)
        self.assertEqual(result.text, long_text[:17])
        self.assertIn('char_limit', result.reasons)

    def test_corrupt_records_do_not_hide_valid_other_sections(self):
        for broken in (b'\0', struct.pack('<I', 67 | (10 << 20)),
                       struct.pack('<I', 67 | (0xfff << 20)),
                       record(67, b'\x00'), record(67, b'\x00\xd8'),
                       record(67, b'\x02\x00' + b'x\x00' * 7)):
            result = self.extract(streams([broken, paragraph('안전하게 읽은 다음 구역')]))
            self.assertEqual(result.text, '안전하게 읽은 다음 구역')
            self.assertIn('section_read_error', result.reasons)

    def test_truncated_or_trailing_compression_is_not_accepted(self):
        good = deflate(paragraph('신청 안내'))
        for broken in (good[:-1], good + b'ignored', deflate(b'') + good, b'not deflate'):
            result = self.extract({**streams(compressed=True), 'BodyText/Section0': broken})
            self.assertFalse(result.text)
            self.assertIn('section_read_error', result.reasons)

    def test_hwp_writer_crc32_size_trailer_must_match_exactly(self):
        body = paragraph('공공기관 공개 안내')
        trailer = struct.pack('<II', zlib.crc32(body), len(body))
        values = streams(compressed=True)
        info = record(16, struct.pack('<H', 1) + b'\0' * 24, 0)
        values['DocInfo'] = deflate(info) + struct.pack('<II', zlib.crc32(info), len(info))
        values['BodyText/Section0'] = deflate(body) + trailer
        result = self.extract(values)
        self.assertEqual(result.text, '공공기관 공개 안내')
        self.assertEqual(result.reasons, [])
        for bad in (trailer[:-1], trailer + b'\0',
                    struct.pack('<II', zlib.crc32(body) ^ 1, len(body)),
                    struct.pack('<II', zlib.crc32(body), len(body) + 1),
                    trailer + deflate(paragraph('추가 숨긴 구역'))):
            result = self.extract({**values, 'BodyText/Section0': deflate(body) + bad})
            self.assertFalse(result.text)
            self.assertIn('section_read_error', result.reasons)

    def test_compression_bomb_is_bounded_before_text_parsing(self):
        with patch.object(hwp, '_MAX_STREAM_BYTES', 1024):
            result = self.extract({**streams(compressed=True),
                                   'BodyText/Section0': deflate(b'x' * 100000)})
        self.assertFalse(result.text)
        self.assertIn('stream_limit', result.reasons)

    def test_limits_for_input_entries_sections_records_and_total_output(self):
        with patch.object(hwp, '_MAX_FILE_BYTES', 10):
            result = hwp.extract_hwp_document(hwp._OLE_MAGIC + b'x' * 512)
        self.assertEqual(result.reasons, ['file_limit'])
        with patch.object(hwp, '_MAX_ENTRIES', 2):
            result = self.extract(streams())
        self.assertEqual(result.reasons, ['container_limit'])
        with patch.object(hwp, '_MAX_SECTIONS', 1):
            result = self.extract(streams([paragraph('첫째'), paragraph('둘째')]))
        self.assertEqual(result.text, '첫째')
        self.assertIn('section_limit', result.reasons)
        with patch.object(hwp, '_MAX_RECORDS', 1):
            result = self.extract(streams([paragraph('첫째') + paragraph('둘째')]))
        self.assertEqual(result.text, '첫째')
        self.assertIn('record_limit', result.reasons)
        with patch.object(hwp, '_MAX_TOTAL_BYTES', len(paragraph('첫째'))):
            result = self.extract(streams([paragraph('첫째'), paragraph('둘째')]))
        self.assertEqual(result.text, '첫째')
        self.assertIn('total_size_limit', result.reasons)

    def test_gaps_and_declared_section_count_are_not_complete(self):
        values = streams([paragraph('첫째'), paragraph('둘째')])
        del values['BodyText/Section0']
        result = self.extract(values)
        self.assertEqual(result.text, '둘째')
        self.assertIn('section_missing', result.reasons)
        self.assertEqual((result.units_total, result.units_examined), (2, 1))

    def test_missing_or_bad_docinfo_is_a_review_not_silent_success(self):
        for value in (None, b'broken', record(16, struct.pack('<H', 1), 0)):
            values = streams()
            if value is None: del values['DocInfo']
            else: values['DocInfo'] = value
            result = self.extract(values)
            self.assertEqual(result.text, '복지 지원 안내')
            self.assertIn('document_structure_review', result.reasons)

    def test_invalid_inputs_and_missing_parser(self):
        for value in (b'', b'not hwp'):
            self.assertEqual(hwp.extract_hwp_document(value).reasons, ['file_read_error'])
        for limit in (0, -1, True):
            with self.assertRaises(ValueError): hwp.extract_hwp_document(b'', max_chars=limit)
        with self.assertRaises(TypeError): hwp.extract_hwp_document('not bytes')
        with patch.dict(sys.modules, {'olefile': None}):
            result = hwp.extract_hwp_document(hwp._OLE_MAGIC + b'\0' * 504)
        self.assertEqual(result.reasons, ['parser_unavailable'])

    def test_declared_huge_stream_is_not_opened(self):
        fake = FakeOle(streams())
        fake.sizes[('BodyText', 'Section0')] = hwp._MAX_STREAM_BYTES + 1
        with patch.dict(sys.modules, {'olefile': types.SimpleNamespace(DEFECT_INCORRECT=30,
                OleFileIO=lambda *a, **kw: fake)}):
            result = hwp.extract_hwp_document(hwp._OLE_MAGIC + b'\0' * 504)
        self.assertIn('stream_limit', result.reasons)
        self.assertNotIn(('BodyText', 'Section0'), fake.reads)

    def test_real_ole_reader_handles_synthetic_compound_streams(self):
        try:
            import olefile
        except ImportError:
            self.skipTest('olefile dependency is not installed')
        self.assertEqual(olefile.__version__, '0.47')
        for compressed in (False, True):
            data = synthetic_compound(streams([paragraph('첫 구역 지원'), paragraph('둘째 구역 신청')],
                                              compressed=compressed))
            result = hwp.extract_hwp_document(data)
            self.assertEqual(result.text, '첫 구역 지원 둘째 구역 신청')
            self.assertEqual(result.reasons, [])
            self.assertEqual((result.units_total, result.units_examined), (2, 2))

    def test_real_ole_reader_rejects_truncated_and_cyclic_containers(self):
        try:
            import olefile
        except ImportError:
            self.skipTest('olefile dependency is not installed')
        data = synthetic_compound(streams())
        cyclic = bytearray(data)
        # Root's child points at root itself, not a legal storage entry.
        struct.pack_into('<I', cyclic, 512 + 76, 0)
        for malformed in (data[:512], data[:700], bytes(cyclic)):
            result = hwp.extract_hwp_document(malformed)
            self.assertFalse(result.text)
            self.assertIn('file_read_error', result.reasons)


if __name__ == '__main__':
    unittest.main()

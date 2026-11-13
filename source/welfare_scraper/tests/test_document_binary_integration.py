"""Actual in-memory parser fixtures; no network, OCR model, or user documents."""
import io
import unittest
import zipfile

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from services.document_text import extract_pdf_document, extract_hwpx_document


def pdf_bytes(streams, password=None):
    writer = PdfWriter()
    for stream in streams:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): font})})
        if stream is not None:
            content = DecodedStreamObject()
            content.set_data(stream)
            page[NameObject('/Contents')] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


TEXT = b'BT /F1 12 Tf 20 200 Td (Benefit 20 USD. Residents only.) Tj ET'


class TestBinaryPdf(unittest.TestCase):
    def test_real_text_and_blank_page(self):
        result = extract_pdf_document(pdf_bytes([TEXT, None]))
        self.assertIn('Residents only.', result.text)
        self.assertEqual(result.reasons, [])
        self.assertEqual((result.units_total, result.units_examined), (2, 2))

    def test_vector_only_page_is_not_mistaken_for_complete_text(self):
        # Drawing operators contain no extractable text, like a scanned page.
        result = extract_pdf_document(pdf_bytes([TEXT, b'0 0 10 10 re f']))
        self.assertIn('Benefit', result.text)
        self.assertIn('no_text_page', result.reasons)

    def test_real_page_limit_keeps_coverage_warning(self):
        result = extract_pdf_document(pdf_bytes([TEXT, TEXT]), max_pages=1)
        self.assertIn('page_limit', result.reasons)
        self.assertEqual((result.units_total, result.units_examined), (2, 1))

    def test_real_character_limit_keeps_coverage_warning(self):
        result = extract_pdf_document(pdf_bytes([TEXT]), max_chars=7)
        self.assertEqual(result.text, 'Benefit')
        self.assertIn('char_limit', result.reasons)

    def test_encrypted_pdf_requires_manual_review(self):
        with self.assertRaises(ValueError):
            extract_pdf_document(pdf_bytes([TEXT], password='fixture-only'))

    def test_corrupt_pdf_is_not_an_empty_success(self):
        with self.assertRaises(ValueError):
            extract_pdf_document(b'%PDF-1.7\nnot-a-document')


class TestBinaryHwpx(unittest.TestCase):
    def test_namespaced_korean_sections_keep_numeric_order_and_conditions(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for number, text in ((10, '방문 신청 필요'), (2, '중복 지원 불가'), (0, '월 2만원 지원')):
                archive.writestr(f'Contents/section{number}.xml',
                    f'<s:sec xmlns:s="urn:section" xmlns:p="urn:paragraph">'
                    f'<p:p><p:run><p:t>{text}</p:t></p:run></p:p></s:sec>')
        result = extract_hwpx_document(output.getvalue())
        self.assertEqual(result.text, '월 2만원 지원 중복 지원 불가 방문 신청 필요')
        self.assertEqual(result.reasons, [])
        self.assertEqual(result.units_examined, 3)

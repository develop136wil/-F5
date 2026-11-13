"""Real PDFium parsing/rasterization; OCR is mocked with synthetic text."""
import io
from unittest.mock import patch

import pytest
from pypdf import PdfWriter
from services.document_text import DocumentText
from services.local_pdf_ocr import extract_scanned_pdf_document


def pdf(pages=1, size=(300, 400), password=None):
    writer=PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=size[0],height=size[1])
    if password:
        writer.encrypt(password)
    buffer=io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def recognized(text='월 20만원. 중복 지원 불가', reasons=None):
    return DocumentText(text=text, reasons=['ocr_verification_required'] if reasons is None else reasons,
                        units_total=1,units_examined=1)


def test_scan_pages_render_to_bytes_and_retain_review():
    with patch('services.local_ocr.extract_image_document',return_value=recognized()) as ocr:
        result=extract_scanned_pdf_document(pdf(2))
    assert ocr.call_count==2
    assert all(call.args[0].startswith(b'\x89PNG') for call in ocr.call_args_list)
    assert '20만원' in result.text and '중복 지원 불가' in result.text
    assert result.reasons==['ocr_verification_required']
    assert (result.units_total,result.units_examined)==(2,2)


def test_six_page_limit_is_visible_and_no_extra_ocr():
    with patch('services.local_ocr.extract_image_document',return_value=recognized()) as ocr:
        result=extract_scanned_pdf_document(pdf(7))
    assert ocr.call_count==6
    assert 'page_limit' in result.reasons
    assert (result.units_total,result.units_examined)==(7,6)


def test_oversized_page_rejected_before_render_or_ocr():
    with patch('services.local_ocr.extract_image_document') as ocr:
        result=extract_scanned_pdf_document(pdf(size=(100000,100000)))
    ocr.assert_not_called()
    assert not result.text and 'image_size_limit' in result.reasons


def test_character_limit_preserves_prefix():
    with patch('services.local_ocr.extract_image_document',return_value=recognized()):
        result=extract_scanned_pdf_document(pdf(2),max_chars=5)
    assert len(result.text)<=5 and 'char_limit' in result.reasons
    assert result.units_examined==1


def test_missing_engine_does_not_retry_every_page():
    with patch('services.local_ocr.extract_image_document',return_value=recognized('', ['ocr_models_unavailable'])) as ocr:
        result=extract_scanned_pdf_document(pdf(3))
    assert ocr.call_count==1
    assert 'ocr_models_unavailable' in result.reasons
    assert not result.text


def test_failed_page_does_not_drop_readable_other_page():
    with patch('services.local_ocr.extract_image_document',side_effect=[ValueError('private'),recognized()]):
        result=extract_scanned_pdf_document(pdf(2))
    assert '20만원' in result.text
    assert 'page_read_error' in result.reasons
    assert 'private' not in str(result.coverage())


@pytest.mark.parametrize('data',[b'broken',b'%PDF-corrupt',pdf(password='not-a-secret')])
def test_invalid_or_encrypted_pdf_never_calls_ocr(data):
    with patch('services.local_ocr.extract_image_document') as ocr:
        result=extract_scanned_pdf_document(data)
    ocr.assert_not_called()
    assert not result.text and 'file_read_error' in result.reasons


@pytest.mark.parametrize('pages',[0,7,True,1.5])
def test_invalid_page_limit(pages):
    with pytest.raises(ValueError):
        extract_scanned_pdf_document(pdf(),max_pages=pages)

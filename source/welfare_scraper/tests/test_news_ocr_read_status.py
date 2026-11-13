import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.document_text import DocumentText
from services import news_attachments as attachments, page_coverage


def read_text(*reasons, total=1, examined=1, text='Family Support 2026 아동 지원 10만원'):
    return DocumentText(text=text, reasons=['ocr_verification_required', *reasons],
                        units_total=total, units_examined=examined)


def test_successful_ocr_is_read_not_an_unreadable_attachment():
    assert attachments.local_ocr_status(read_text()) == 'ocr_text_extracted'
    summary = attachments.summarize_attachment_reports([dict(status='ocr_text_extracted',
        coverage=read_text().coverage(), kind='png', filename='공고.png')])
    assert summary['analyzed'] == 1
    assert summary['status'] == 'complete'
    assert summary['note'] == ''
    assert 'PC에서' in summary['files'][0]['label']


@pytest.mark.parametrize('reason', ['ocr_low_confidence', 'ocr_omitted_text', 'char_limit',
    'page_limit', 'page_read_error', 'no_text_page', 'future_unknown_warning'])
def test_real_ocr_uncertainty_still_requires_review(reason):
    assert attachments.local_ocr_status(read_text(reason)) == 'partial_extracted'


@pytest.mark.parametrize('total,examined', [(0, 0), (2, 1), (1, 0)])
def test_missing_coverage_cannot_claim_ocr_success(total, examined):
    assert attachments.local_ocr_status(read_text(total=total, examined=examined)) == 'partial_extracted'


def test_empty_ocr_is_still_pending():
    assert attachments.local_ocr_status(read_text(text='')) == 'ocr_required'


def test_image_and_scanned_pdf_success_keep_method_not_generic_warning():
    for kind in ('png', 'pdf'):
        outputs = [read_text()] if kind == 'png' else [DocumentText(reasons=['no_text_page']), read_text()]
        with patch.object(attachments, 'extract_document_isolated', AsyncMock(side_effect=outputs)):
            result = asyncio.run(attachments._extract_downloaded_inner(dict(kind=kind, data=b'fixture')))
        assert result['status'] == 'ocr_text_extracted'
        assert 'Family Support' in result['text']
        assert result['coverage']['reasons'] == ['ocr_verification_required']


def test_method_metadata_never_overrides_real_coverage_warning():
    for unknown in (None, {}, {'status': 'complete'}, {'reasons': 'unknown'}):
        assert page_coverage.with_methods(unknown, 'local_ocr') == {}
    old = page_coverage.make()
    assert 'methods' not in old
    read = page_coverage.with_methods(old, 'local_ocr', 'untrusted', 'local_ocr')
    assert read['methods'] == ['local_ocr'] and read['status'] == 'complete'
    partial = page_coverage.with_reasons(read, 'attachment_review')
    assert partial['status'] == 'review_required' and partial['methods'] == ['local_ocr']
    assert page_coverage.with_methods(partial, 'local_ocr')['status'] == 'review_required'


def test_result_method_is_captured_input_only_and_does_not_upgrade_status():
    from services.news_analysis_store import _source_review
    source = dict(content='본문', source_page=page_coverage.with_methods(page_coverage.make('page_char_limit'), 'local_ocr'))
    result = _source_review(source, {})
    assert result['status'] == 'partial' and result['methods'] == ['local_ocr']
    assert 'methods' not in _source_review(dict(content='본문', source_page=page_coverage.make()),
        dict(source_page=page_coverage.with_methods(page_coverage.make(), 'local_ocr')))

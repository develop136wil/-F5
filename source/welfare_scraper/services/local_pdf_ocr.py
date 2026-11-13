"""Bounded image-PDF OCR in the disposable worker, without remote AI."""
import io
import math

from services.document_text import DocumentText, TextBudget


def extract_scanned_pdf_document(data, *, max_chars=30000, max_pages=6):
    result, budget = DocumentText(max_chars=max_chars), TextBudget(max_chars)
    if type(max_pages) is not int or not 1 <= max_pages <= 6:
        raise ValueError('Invalid OCR page limit')
    if not isinstance(data, bytes) or not 0 < len(data) <= 20 * 1024 * 1024:
        result.mark('input_limit')
        return result
    try:
        import pypdfium2 as pdfium
        from services.local_ocr import extract_image_document
    except ImportError:
        result.mark('parser_unavailable')
        return result
    try:
        document = pdfium.PdfDocument(data)
        try:
            result.units_total = len(document)
            if result.units_total > max_pages:
                result.mark('page_limit')
            for index in range(min(result.units_total, max_pages)):
                result.units_examined += 1
                page = bitmap = image = None
                try:
                    page = document[index]
                    width, height = page.get_size()
                    scale = 2.5  # 180 DPI, with an explicit pre-allocation cap.
                    if (not all(math.isfinite(x) and x > 0 for x in (width, height))
                            or max(width, height) * scale > 6000
                            or math.ceil(width * scale) * math.ceil(height * scale) > 20000000):
                        result.mark('image_size_limit')
                        continue
                    bitmap = page.render(scale=scale, rev_byteorder=True)
                    image = bitmap.to_pil()
                    output = io.BytesIO()
                    image.save(output, format='PNG')
                    extracted = extract_image_document(output.getvalue(), max_chars=max_chars)
                    for reason in extracted.reasons:
                        result.mark(reason)
                    if not extracted.text:
                        result.mark('no_text_page')
                    if not budget.append(extracted.text):
                        result.mark('char_limit')
                        break
                    if any(reason in extracted.reasons for reason in
                           ('ocr_unavailable', 'ocr_models_unavailable', 'ocr_model_unavailable', 'ocr_model_invalid', 'parser_unavailable')):
                        break  # Do not repeatedly initialize an unavailable engine.
                except Exception:
                    result.mark('page_read_error')
                finally:
                    if image is not None:
                        image.close()
                    if bitmap is not None:
                        bitmap.close()
                    if page is not None:
                        page.close()
        finally:
            document.close()
    except Exception:
        result.mark('file_read_error')
    result.text = budget.text()
    return result

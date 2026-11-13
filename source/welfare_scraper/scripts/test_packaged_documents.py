"""Offline smoke of the *actual* packaged document worker using generated files.

Run from this checkout: python scripts/test_packaged_documents.py PATH_TO_EXE
Uses the production worker's memory/time limits and cleanup. No application,
database, credentials, user documents, cloud OCR, or runtime downloads are used.
This verifies extraction availability, not general OCR accuracy. OCR results must
still carry the original-verification warning. HWPX has no worker mode and is
covered separately by test_document_binary_integration/test_document_coverage.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import local_document_process as worker
from services.local_document_worker import _block_python_network
from tests.test_document_binary_integration import TEXT, pdf_bytes
from tests.test_hwp_text import paragraph, streams, synthetic_compound


def generated_images(font_path=None):
    """Only a system/explicit local font is read; both documents are synthetic."""
    from PIL import Image, ImageDraw, ImageFont

    candidates = ([Path(font_path)] if font_path else [
        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'malgun.ttf',
        Path('/usr/share/fonts/truetype/nanum/NanumGothic.ttf'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
    ])
    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        raise ValueError('A local Korean font is required; provide --font PATH')
    font = ImageFont.truetype(str(selected), 56)
    with Image.new('RGB', (1400, 480), 'white') as image:
        draw = ImageDraw.Draw(image)
        for y, text in ((50, '복지 지원 안내'), (180, 'BENEFIT SUPPORT 2026'), (310, '중복 지원 불가')):
            draw.text((60, y), text, fill='black', font=font)
        png, scanned = io.BytesIO(), io.BytesIO()
        image.save(png, format='PNG')
        image.save(scanned, format='PDF', resolution=180)
    return png.getvalue(), scanned.getvalue(), selected.name


def _check(result, *, expected, ocr):
    normalized = ''.join(result.text.split()).casefold()
    missing = [phrase for phrase in expected if ''.join(phrase.split()).casefold() not in normalized]
    errors = []
    if missing:
        errors.append('expected_text_missing')
    if (result.units_total, result.units_examined) != (1, 1):
        errors.append('unexpected_coverage')
    if ocr:
        if 'ocr_verification_required' not in result.reasons:
            errors.append('ocr_review_warning_missing')
        # Confidence warnings are evidence, not an unavailable parser/model.
        allowed = {'ocr_verification_required', 'ocr_low_confidence', 'ocr_omitted_text'}
        if set(result.reasons) - allowed:
            errors.append('extraction_failed_or_incomplete')
    elif result.reasons:
        errors.append('unexpected_extraction_warning')
    return errors, missing


async def run(executable, *, font_path=None, json_only=False):
    executable = Path(executable).resolve(strict=True)
    if not executable.is_file():
        raise ValueError('The packaged backend executable must be a file')
    # Parent fixture generation and the child worker both deny Python networking.
    # The ONNX adapter independently disables telemetry and runtime downloads.
    # This is not an OS firewall claim.
    _block_python_network()
    png, scanned, font_name = generated_images(font_path)
    hwp_text = '복지 지원 안내 BENEFIT SUPPORT 2026 중복 지원 불가'
    cases = [
        ('hwp_plain', 'hwp', synthetic_compound(streams([paragraph(hwp_text)])),
         [hwp_text], False),
        ('hwp_compressed', 'hwp', synthetic_compound(streams([paragraph(hwp_text)], compressed=True)),
         [hwp_text], False),
        ('pdf_text', 'pdf_text', pdf_bytes([TEXT]), ['Benefit 20 USD.', 'Residents only.'], False),
        ('image_ocr', 'image', png, ['복지 지원', '중복 지원 불가', 'BENEFIT SUPPORT'], True),
        ('scanned_pdf_ocr', 'pdf_ocr', scanned, ['복지 지원', '중복 지원 불가', 'BENEFIT SUPPORT'], True),
    ]
    results = []
    for name, kind, payload, expected, ocr in cases:
        folders = []
        def command(folder, document_kind, max_chars):
            folders.append(folder)
            return [str(executable), '--local-document-worker', str(folder), document_kind, str(max_chars)]
        started = time.monotonic()
        with patch.object(worker, '_command', side_effect=command):
            result = await worker.extract_document_isolated(payload, kind)
        errors, missing = _check(result, expected=expected, ocr=ocr)
        cleaned = bool(folders) and all(not folder.exists() for folder in folders)
        if not cleaned:
            errors.append('worker_temporary_files_not_cleaned')
        item = dict(case=name, kind=kind, passed=not errors,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    timeout_seconds=worker.TIMEOUTS[kind],
                    input_bytes=len(payload), text=result.text,
                    reasons=result.reasons, units_total=result.units_total,
                    units_examined=result.units_examined, temporary_files_cleaned=cleaned,
                    errors=errors, missing_expected_text=missing)
        results.append(item)
        if not json_only:
            print(f"{'PASS' if item['passed'] else 'FAIL'}: {name} "
                  f"({item['elapsed_seconds']}s, {len(result.text)} chars, "
                  f"reasons={result.reasons}, cleaned={cleaned})", flush=True)
    report = dict(schema_version=1, executable=str(executable), generated_documents_only=True,
                  python_network_denied=True, os_network_sandbox=False,
                  application_start_requested=False, font=font_name,
                  passed=all(item['passed'] for item in results), cases=results)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['passed'] else 1


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', help='Path to the built api-server.exe')
    parser.add_argument('--font', help='Optional local Korean-capable font file')
    parser.add_argument('--json', action='store_true', help='Emit only the final JSON report')
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.executable, font_path=args.font, json_only=args.json))
    except (OSError, ValueError, ImportError) as error:
        # Paths/documents are generated or explicitly supplied, but never echo
        # arbitrary dependency exceptions or a possibly inherited environment.
        print(json.dumps(dict(schema_version=1, passed=False,
                              setup_error=type(error).__name__), ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

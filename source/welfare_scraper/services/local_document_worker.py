"""Private local extraction entry point, before any database or app logging imports."""
import json
import os
from pathlib import Path
import sys
import time


def _block_python_network():
    # Defence in depth for Python libraries; not an OS-level network sandbox.
    import socket
    def denied(*_args, **_kwargs):
        raise OSError('Document worker network is disabled')
    socket.create_connection = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    socket.getaddrinfo = denied


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 3:
        return 2
    folder = Path(arguments[0])
    kind = arguments[1]
    try:
        max_chars = int(arguments[2])
        if (kind not in ('hwp', 'image', 'pdf_text', 'pdf_ocr') or not 1 <= max_chars <= 30000
                or not folder.is_absolute() or not folder.name.startswith('welfare-f5-document-')
                or not folder.is_dir() or folder.is_symlink()):
            return 2
        deadline = time.monotonic() + 10
        while not (folder / 'start').is_file():
            if time.monotonic() >= deadline:
                return 3
            time.sleep(.02)
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
        payload = folder / 'payload.bin'
        if payload.is_symlink() or not 0 < payload.stat().st_size <= 20 * 1024 * 1024:
            return 2
        data = payload.read_bytes()
        _block_python_network()
        from services.document_text import DocumentText
        try:
            if kind == 'hwp':
                from services.hwp_text import extract_hwp_document
                result = extract_hwp_document(data, max_chars=max_chars)
            elif kind == 'image':
                from services.local_ocr import extract_image_document
                result = extract_image_document(data, max_chars=max_chars)
            elif kind == 'pdf_text':
                from services.document_text import extract_pdf_document
                result = extract_pdf_document(data, max_chars=max_chars)
            else:
                from services.local_pdf_ocr import extract_scanned_pdf_document
                result = extract_scanned_pdf_document(data, max_chars=max_chars)
        except ImportError:
            result = DocumentText(max_chars=max_chars, reasons=['parser_unavailable'])
        except Exception:
            result = DocumentText(max_chars=max_chars, reasons=['file_read_error'])
        with (folder / 'result.json').open('x', encoding='utf-8') as stream:
            json.dump({'text': result.text, **result.coverage()}, stream, ensure_ascii=False)
        return 0
    except (OSError, ValueError, TypeError):
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

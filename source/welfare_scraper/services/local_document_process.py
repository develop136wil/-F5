"""Bounded disposable document worker. Never starts the app or sends documents to AI."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from services.document_text import DocumentText

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
TIMEOUTS = {'hwp': 15, 'image': 60, 'pdf_text': 30, 'pdf_ocr': 120}


class _WorkerJob:
    """Own only this worker and cap committed memory; not an OS security sandbox."""
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                        ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('ReadOperations', 'WriteOperations', 'OtherOperations',
                         'ReadBytes', 'WriteBytes', 'OtherBytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Io),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api = api
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError('worker_job_unavailable')
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100  # kill-on-close, process memory
        limits.ProcessMemoryLimit = MEMORY_LIMIT_BYTES
        if (not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle)))):
            self.close()
            raise OSError('worker_job_unavailable')

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _command(folder, kind, max_chars):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--local-document-worker', str(folder), kind, str(max_chars)]
    return [sys.executable, '-m', 'services.local_document_worker', str(folder), kind, str(max_chars)]


def _worker_environment(folder):
    # No Google/Notion keys, inherited proxy, arbitrary plugin or user package setup.
    permitted = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROCESSOR_ARCHITECTURE',
                 'NUMBER_OF_PROCESSORS', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
                 'PATH', 'PYTHONPATH', 'VIRTUAL_ENV'}
    env = {key: value for key, value in os.environ.items()
           if key.upper() in permitted or key.startswith('_PYI_')}
    env.update(TEMP=str(folder), TMP=str(folder), USERPROFILE=str(folder),
               LOCALAPPDATA=str(folder), APPDATA=str(folder), PYTHONIOENCODING='utf-8',
               PYTHONUTF8='1', PYTHONNOUSERSITE='1', OMP_NUM_THREADS='2',
               OPENBLAS_NUM_THREADS='2', MKL_NUM_THREADS='2')
    return env


def _decode_result(folder, max_chars):
    path = folder / 'result.json'
    if not path.is_file() or path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError('invalid_worker_result')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) != {'text', 'reasons', 'units_total', 'units_examined', 'max_chars'}:
        raise ValueError('invalid_worker_result')
    if (not isinstance(value['text'], str) or len(value['text']) > max_chars
            or not isinstance(value['reasons'], list) or len(value['reasons']) > 32
            or any(not isinstance(x, str) or len(x) > 80 or not x.replace('_', '').isalnum()
                   for x in value['reasons'])
            or any(type(value[x]) is not int or not 0 <= value[x] <= 100000
                   for x in ('units_total', 'units_examined'))
            or type(value['max_chars']) is not int or value['max_chars'] != max_chars
            or value['units_examined'] > value['units_total']):
        raise ValueError('invalid_worker_result')
    return DocumentText(**value)


async def extract_document_isolated(data, kind, *, max_chars=30000, timeout=None):
    if kind not in TIMEOUTS or type(max_chars) is not int or not 1 <= max_chars <= 30000:
        raise ValueError('invalid_document_request')
    result = DocumentText(max_chars=max_chars)
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        result.mark('input_limit' if isinstance(data, bytes) and len(data) > MAX_INPUT_BYTES else 'file_read_error')
        return result
    folder = Path(tempfile.mkdtemp(prefix='welfare-f5-document-'))
    process = job = None
    try:
        (folder / 'payload.bin').write_bytes(data)
        process = subprocess.Popen(_command(folder, kind, max_chars),
            cwd=Path(__file__).resolve().parents[1], env=_worker_environment(folder),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job = _WorkerJob(process)
        # Worker imports no parser/ML engine before the resource job is assigned.
        (folder / 'start').touch(exist_ok=False)
        deadline = time.monotonic() + (TIMEOUTS[kind] if timeout is None else min(timeout, TIMEOUTS[kind]))
        while process.poll() is None:
            if time.monotonic() >= deadline:
                result.mark('extraction_timeout')
                return result
            await asyncio.sleep(.05)
        if process.returncode:
            result.mark('worker_failed')
            return result
        return _decode_result(folder, max_chars)
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, TypeError):
        result.mark('worker_unavailable')
        return result
    finally:
        if job is not None:
            job.close()
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # Exact freshly created directory only. Never a user document or app data folder.
        if process is None or process.poll() is not None:
            shutil.rmtree(folder, ignore_errors=True)

"""Private worker contract: no operating app startup, cloud calls, or real documents."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import local_document_process as process_module
from services import local_document_worker as worker_module


def result_payload(**changes):
    value = {'text': '로컬 문서', 'reasons': [], 'units_total': 1,
             'units_examined': 1, 'max_chars': 30}
    value.update(changes)
    return value


def write_result(folder, value=None):
    (folder / 'result.json').write_text(
        json.dumps(result_payload() if value is None else value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def private_folder(tmp_path, monkeypatch):
    folder = tmp_path / '한글 문서 임시 경로' / 'welfare-f5-document-test'
    folder.mkdir(parents=True)
    monkeypatch.setattr(process_module.tempfile, 'mkdtemp', lambda **kwargs: str(folder))
    return folder


def mock_child(monkeypatch, folder, *, running=False, code=0, payload=None, job_error=False):
    child = Mock()
    child.returncode = None if running else code
    child.poll.side_effect = lambda: child.returncode
    child.kill.side_effect = lambda: setattr(child, 'returncode', -9)
    child.wait.return_value = -9
    job = Mock()
    def popen(*args, **kwargs):
        assert (folder / 'payload.bin').read_bytes() == b'synthetic invalid HWP'
        assert not (folder / 'start').exists()
        if not running and code == 0:
            write_result(folder, payload)
        return child
    spawn = Mock(side_effect=popen)
    def job_factory(value):
        assert value is child
        assert not (folder / 'start').exists(), 'parser must wait until resource limits are assigned'
        if job_error:
            raise OSError('worker_job_unavailable')
        return job
    monkeypatch.setattr(process_module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(process_module, '_WorkerJob', Mock(side_effect=job_factory))
    return child, job, spawn


def test_source_command_passes_unicode_path_as_one_argument(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    folder = tmp_path / '한글 공백 경로'
    assert process_module._command(folder, 'hwp', 30) == [
        sys.executable, '-m', 'services.local_document_worker', str(folder), 'hwp', '30']


def test_frozen_command_uses_early_worker_switch_not_python_module(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', 'C:/설치 폴더/복지F5.exe')
    folder = tmp_path / '한글 공백 경로'
    assert process_module._command(folder, 'image', 30) == [
        sys.executable, '--local-document-worker', str(folder), 'image', '30']


def test_child_environment_omits_credentials_proxies_and_operating_data_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('NOTION_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('HTTP_PROXY', 'http://invalid.test')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', 'private-file.json')
    monkeypatch.setenv('PYTHONSTARTUP', 'unexpected.py')
    monkeypatch.setenv('PYTHONPATH', 'explicit-test-dependencies')
    monkeypatch.setenv('_PYI_APPLICATION_HOME_DIR', 'frozen-runtime')
    value = process_module._worker_environment(tmp_path)
    for key in ('GEMINI_API_KEY', 'NOTION_TOKEN', 'HTTP_PROXY',
                'GOOGLE_APPLICATION_CREDENTIALS', 'PYTHONSTARTUP'):
        assert key not in value
    for key in ('TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA'):
        assert value[key] == str(tmp_path)
    assert value['PYTHONNOUSERSITE'] == '1'
    assert value['PYTHONIOENCODING'] == 'utf-8'
    assert value['PYTHONPATH'] == 'explicit-test-dependencies'
    assert value['_PYI_APPLICATION_HOME_DIR'] == 'frozen-runtime'


def test_success_closes_job_removes_private_files_and_spawns_hidden(private_folder, monkeypatch):
    child, job, spawn = mock_child(monkeypatch, private_folder)
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', 'hwp', max_chars=30))
    assert value.text == '로컬 문서'
    assert value.reasons == []
    job.close.assert_called_once_with()
    child.kill.assert_not_called()
    child.wait.assert_not_called()
    assert not private_folder.exists()
    arguments = spawn.call_args.kwargs
    assert arguments['stdin'] == subprocess.DEVNULL
    assert arguments['stdout'] == subprocess.DEVNULL
    assert arguments['stderr'] == subprocess.DEVNULL
    assert arguments['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    assert arguments.get('shell', False) is False


@pytest.mark.parametrize('kind', ['hwp', 'pdf_text'])
def test_timeout_closes_job_kills_waits_and_cleans_private_folder(private_folder, monkeypatch, kind):
    child, job, _ = mock_child(monkeypatch, private_folder, running=True)
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', kind, max_chars=30, timeout=0))
    assert value.reasons == ['extraction_timeout']
    job.close.assert_called_once_with()
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=5)
    assert not private_folder.exists()


@pytest.mark.parametrize('kind', ['hwp', 'pdf_text'])
def test_cancellation_propagates_only_after_worker_cleanup(private_folder, monkeypatch, kind):
    child, job, _ = mock_child(monkeypatch, private_folder, running=True)
    async def cancel_sleep(_delay):
        raise asyncio.CancelledError()
    monkeypatch.setattr(process_module.asyncio, 'sleep', cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(process_module.extract_document_isolated(
            b'synthetic invalid HWP', kind, max_chars=30))
    job.close.assert_called_once_with()
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=5)
    assert not private_folder.exists()


def test_failure_to_assign_resource_job_does_not_start_parser(private_folder, monkeypatch):
    child, _job, _ = mock_child(monkeypatch, private_folder, running=True, job_error=True)
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', 'hwp', max_chars=30))
    assert value.reasons == ['worker_unavailable']
    child.kill.assert_called_once_with()
    child.wait.assert_called_once_with(timeout=5)
    assert not private_folder.exists()


def test_nonzero_worker_exit_is_reviewable_failure_not_empty_success(private_folder, monkeypatch):
    _child, job, _ = mock_child(monkeypatch, private_folder, code=2)
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', 'hwp', max_chars=30))
    assert value.text == ''
    assert value.reasons == ['worker_failed']
    job.close.assert_called_once_with()
    assert not private_folder.exists()


def test_bad_child_schema_becomes_reviewable_failure_and_is_cleaned(private_folder, monkeypatch):
    _child, job, _ = mock_child(monkeypatch, private_folder, payload={'text': 'untrusted'})
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', 'hwp', max_chars=30))
    assert value.reasons == ['worker_unavailable']
    job.close.assert_called_once_with()
    assert not private_folder.exists()


@pytest.mark.parametrize('kind,max_chars', [('unknown', 30), ('hwp', 0), ('hwp', 30001),
                                           ('hwp', True), ('hwp', 30.0)])
def test_bad_requests_do_not_create_files_or_process(kind, max_chars, monkeypatch):
    spawn, create = Mock(), Mock()
    monkeypatch.setattr(process_module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(process_module.tempfile, 'mkdtemp', create)
    with pytest.raises(ValueError, match='invalid_document_request'):
        asyncio.run(process_module.extract_document_isolated(b'bad', kind, max_chars=max_chars))
    spawn.assert_not_called()
    create.assert_not_called()


@pytest.mark.parametrize('data,reason', [(b'', 'file_read_error'), ('not bytes', 'file_read_error'),
                                       (b'x' * 9, 'input_limit')])
def test_invalid_input_is_rejected_before_worker_creation(data, reason, monkeypatch):
    monkeypatch.setattr(process_module, 'MAX_INPUT_BYTES', 8)
    spawn, create = Mock(), Mock()
    monkeypatch.setattr(process_module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(process_module.tempfile, 'mkdtemp', create)
    value = asyncio.run(process_module.extract_document_isolated(data, 'hwp'))
    assert value.reasons == [reason]
    spawn.assert_not_called()
    create.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'text': None}, {'text': 'x' * 31}, {'reasons': 'not a list'},
    {'reasons': ['file read error']}, {'reasons': ['x' * 81]},
    {'reasons': ['reason'] * 33}, {'reasons': [None]},
    {'units_total': True}, {'units_examined': False}, {'units_total': -1},
    {'units_examined': 100001}, {'units_total': 1.0}, {'units_examined': 1.5},
    {'max_chars': 31}, {'max_chars': 30.0}, {'units_total': 0, 'units_examined': 1},
])
def test_invalid_result_fields_are_rejected(tmp_path, changes):
    write_result(tmp_path, result_payload(**changes))
    with pytest.raises(ValueError, match='invalid_worker_result'):
        process_module._decode_result(tmp_path, 30)


@pytest.mark.parametrize('value', [[], None, {'text': 'only field'}, result_payload(extra='ignored')])
def test_result_requires_exact_object_schema(tmp_path, value):
    (tmp_path / 'result.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='invalid_worker_result'):
        process_module._decode_result(tmp_path, 30)


def test_missing_oversized_and_malformed_result_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='invalid_worker_result'):
        process_module._decode_result(tmp_path, 30)
    monkeypatch.setattr(process_module, 'MAX_RESULT_BYTES', 12)
    write_result(tmp_path)
    with pytest.raises(ValueError, match='invalid_worker_result'):
        process_module._decode_result(tmp_path, 30)
    (tmp_path / 'result.json').write_text('{bad json', encoding='utf-8')
    with pytest.raises(ValueError):
        process_module._decode_result(tmp_path, 30)


def test_valid_incomplete_coverage_is_preserved_exactly(tmp_path):
    write_result(tmp_path, result_payload(reasons=['ocr_unverified', 'page_limit'],
                                         units_total=5, units_examined=2))
    value = process_module._decode_result(tmp_path, 30)
    assert value.text == '로컬 문서'
    assert value.reasons == ['ocr_unverified', 'page_limit']
    assert (value.units_total, value.units_examined, value.max_chars) == (5, 2, 30)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object contract')
@pytest.mark.parametrize('fail_step', [None, 'create', 'set_limits', 'assign'])
def test_windows_job_memory_kill_on_close_and_failure_cleanup(monkeypatch, fail_step):
    api = SimpleNamespace(CreateJobObjectW=Mock(return_value=None if fail_step == 'create' else 123),
                          SetInformationJobObject=Mock(return_value=fail_step != 'set_limits'),
                          AssignProcessToJobObject=Mock(return_value=fail_step != 'assign'),
                          CloseHandle=Mock(return_value=True))
    monkeypatch.setattr(process_module.ctypes, 'WinDLL', Mock(return_value=api))
    child = SimpleNamespace(_handle=456)
    if fail_step:
        with pytest.raises(OSError, match='worker_job_unavailable'):
            process_module._WorkerJob(child)
        assert api.CloseHandle.call_count == (0 if fail_step == 'create' else 1)
        return
    job = process_module._WorkerJob(child)
    limits = api.SetInformationJobObject.call_args.args[2]._obj
    assert limits.BasicLimitInformation.LimitFlags == 0x2000 | 0x100
    assert limits.ProcessMemoryLimit == process_module.MEMORY_LIMIT_BYTES
    assert api.AssignProcessToJobObject.call_args.args[1].value == 456
    job.close()
    job.close()
    api.CloseHandle.assert_called_once_with(123)
    assert job.handle is None


@pytest.mark.parametrize('arguments', [[], ['relative', 'hwp', '30'],
                                       ['relative', 'invalid', '30'], ['relative', 'hwp', 'NaN']])
def test_worker_bad_arguments_exit_before_any_parser(arguments):
    assert worker_module.main(arguments) == 2


def test_actual_worker_uses_unicode_private_path_and_returns_safe_bad_hwp_result(private_folder):
    value = asyncio.run(process_module.extract_document_isolated(
        b'synthetic invalid HWP', 'hwp', max_chars=30, timeout=10))
    assert value.text == ''
    assert value.reasons == ['file_read_error']
    assert value.max_chars == 30
    assert not private_folder.exists()


@pytest.mark.parametrize('entry', ['module', 'api_worker_switch'])
@pytest.mark.parametrize('kind', ['hwp', 'pdf_text'])
def test_actual_child_never_imports_database_app_or_logs(tmp_path, entry, kind):
    folder = tmp_path / '한글 경로 공백' / 'welfare-f5-document-real-child'
    folder.mkdir(parents=True)
    (folder / 'payload.bin').write_bytes(b'synthetic invalid HWP')
    (folder / 'start').touch()
    # A successful guarded child proves it exits before normal app imports.
    bootstrap = """
import importlib.abc, pathlib, runpy, sys
blocked = {'database', 'utils', 'logger', 'fastapi', 'ai_classifier', 'ocr_service'}
class RejectApp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise RuntimeError('forbidden_app_import:' + fullname)
sys.meta_path.insert(0, RejectApp())
entry, folder, kind = sys.argv[1:]
if entry == 'module':
    sys.argv = ['services.local_document_worker', folder, kind, '30']
    runpy.run_module('services.local_document_worker', run_name='__main__')
else:
    sys.argv = ['api_server.py', '--local-document-worker', folder, kind, '30']
    runpy.run_path('api_server.py', run_name='__main__')
"""
    completed = subprocess.run([sys.executable, '-c', bootstrap, entry, str(folder), kind],
        cwd=Path(process_module.__file__).resolve().parents[1],
        env=process_module._worker_environment(folder), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ''
    value = process_module._decode_result(folder, 30)
    assert value.reasons == ['file_read_error']
    assert sorted(path.name for path in folder.iterdir()) == ['payload.bin', 'result.json', 'start']
    assert not list(tmp_path.rglob('*.db'))
    assert not list(tmp_path.rglob('*.log'))


@pytest.mark.parametrize('case', ['text', 'scan', 'page_limit', 'encrypted', 'corrupt'])
def test_actual_pdf_text_worker_preserves_existing_parser_contract(private_folder, case):
    from tests.test_document_binary_integration import pdf_bytes, TEXT
    streams = [TEXT] * 81 if case == 'page_limit' else ([b'0 0 10 10 re f'] if case == 'scan' else [TEXT])
    data = b'%PDF-1.7\nnot-a-document' if case == 'corrupt' else pdf_bytes(
        streams, password='synthetic-only' if case == 'encrypted' else None)
    value = asyncio.run(process_module.extract_document_isolated(data, 'pdf_text', timeout=20))
    if case in ('encrypted', 'corrupt'):
        assert value.text == ''
        assert value.reasons == ['file_read_error']
    elif case == 'scan':
        assert value.text == ''
        assert value.reasons == ['no_text_page']
        assert (value.units_total, value.units_examined) == (1, 1)
    else:
        assert 'Residents only.' in value.text
        assert value.reasons == (['page_limit'] if case == 'page_limit' else [])
        assert (value.units_total, value.units_examined) == ((81, 80) if case == 'page_limit' else (1, 1))
    assert not private_folder.exists()

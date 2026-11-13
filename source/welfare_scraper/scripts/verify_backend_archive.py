"""Compare a local PyInstaller backend with source without executing the app.

Run with the same Python/PyInstaller environment that built the archive. This
checks packaged Python code, not native dependencies or runtime behaviour.
"""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import types


REQUIRED_MODULES = frozenset({
    'database', 'utils', 'services.ai_coordinator',
    'services.news_analysis_safety', 'services.news_workflow_ownership',
    'services.news_profiles_api', 'services.news_profile_workflow',
    'services.scraper_service',
    'services.ai_input_safety', 'services.ai_rate_policy', 'services.search_dates', 'services.policy_evidence',
    # Release-critical first-use, verified-input and stale-write protections must
    # exist in the frozen archive, not just match when accidentally included.
    'services.ai_verified_input', 'services.ocr_service', 'services.policy_sync',
    'services.policy_undo', 'services.policy_full_check', 'services.news_default_condition',
    'services.news_record_retention', 'services.news_site_collector',
    'services.news_unified_feed', 'services.news_legacy_recovery',
    'services.policy_full_check_budget', 'services.legacy_news_analysis',
    'services.site_test_jobs',
    'services.news_readable_content', 'services.news_display_projection',
    'services.board_detail_links', 'services.policy_review_store',
})


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def normalized(code):
    if isinstance(code, types.CodeType):
        return code.replace(co_filename='', co_consts=tuple(normalized(v) for v in code.co_consts))
    if isinstance(code, tuple):
        return tuple(normalized(v) for v in code)
    return code


def verify_archive(executable, source_root, *, reader_factory=None):
    executable, source_root = Path(executable).resolve(), Path(source_root).resolve()
    before = digest(executable)
    if reader_factory is None:
        from PyInstaller.archive.readers import CArchiveReader
        reader_factory = CArchiveReader
    archive = reader_factory(str(executable))
    pyz_names = [name for name, row in archive.toc.items() if row[-1] == 'z']
    if len(pyz_names) != 1:
        raise ValueError('Expected exactly one Python module archive')
    pyz = archive.open_embedded_archive(pyz_names[0])
    checked = []

    def compare(name, source, packed):
        if not isinstance(packed, types.CodeType):
            raise ValueError(f'No Python code found for {name}')
        raw = source.read_bytes()
        expected = compile(raw, str(source), 'exec', dont_inherit=True, optimize=0)
        checked.append(dict(module=name, source=source.relative_to(source_root).as_posix(),
                            source_sha256=hashlib.sha256(raw).hexdigest(),
                            code_equal=normalized(packed) == normalized(expected)))

    for name in sorted(pyz.toc):
        # Never interpret archive names as arbitrary filesystem paths.
        parts = name.split('.')
        if not all(part.isidentifier() for part in parts):
            raise ValueError('Invalid module name in Python archive')
        relative = Path(*parts)
        source = next((path for path in (source_root / relative.with_suffix('.py'),
                       source_root / relative / '__init__.py') if path.is_file()), None)
        if source is not None:
            compare(name, source, pyz.extract(name))
    compare('api_server', source_root / 'api_server.py', marshal.loads(archive.extract('api_server')))
    missing = sorted(REQUIRED_MODULES - {row['module'] for row in checked})
    mismatched = [row['module'] for row in checked if not row['code_equal']]
    changed = [row['source'] for row in checked
               if digest(source_root / row['source']) != row['source_sha256']]
    environment_files_absent = not any(
        part.casefold().startswith('.env')
        for name in archive.toc for part in name.replace('\\', '/').split('/'))
    binary_unchanged = before == digest(executable)
    return dict(executable=str(executable), sha256=before, bytes=executable.stat().st_size,
                local_modules=checked, packaged_modules=len(pyz.toc),
                missing=missing, mismatched=mismatched, changed_sources=changed,
                environment_files_absent=environment_files_absent,
                binary_unchanged=binary_unchanged,
                executable_launched=False, runtime_verified=False,
                passed=not missing and not mismatched and not changed
                       and environment_files_absent and binary_unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--source-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--report', help='New JSON report path; existing reports are never overwritten')
    args = parser.parse_args()
    result = verify_archive(args.executable, args.source_root)
    if args.report:
        with Path(args.report).open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    summary = {key: value for key, value in result.items() if key != 'local_modules'}
    summary['local_modules_checked'] = len(result['local_modules'])
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

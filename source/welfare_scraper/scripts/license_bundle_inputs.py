"""Check and collect only the explicitly audited, pinned notice files.

This build check is not a complete license/compliance audit. It performs no
network access and never imports application config, credentials or databases.
"""
import argparse
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path, PurePosixPath


REQUIRED_TARGETS = {
    'assets/licenses/onnxruntime-1.30.0/LICENSE',
    'assets/licenses/onnxruntime-1.30.0/ThirdPartyNotices.txt',
    'assets/licenses/pandas-2.3.3/LICENSE',
    'assets/licenses/pypdf-6.16.2/LICENSE',
    'assets/licenses/fastapi-0.128.7/LICENSE',
    'assets/licenses/PyYAML-6.0.3/LICENSE',
    'assets/licenses/antlr4-python3-runtime-4.9.3/LICENSE.txt',
}


def _relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in value or ':' in value:
        raise RuntimeError('License input paths must stay within their declared roots')
    return path


def checked_license_inputs(spec_root, *, distribution_lookup=distribution):
    root = Path(spec_root).resolve() / 'assets' / 'licenses'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    files = manifest.get('files', [])
    if (manifest.get('version') != 1 or len(files) != len(REQUIRED_TARGETS)
            or {entry.get('target') for entry in files} != REQUIRED_TARGETS):
        raise RuntimeError('Pinned license inventory is missing or invalid')
    checked = []
    for entry in files:
        source, target = _relative(entry['source']), _relative(entry['target'])
        package = distribution_lookup(entry['distribution'])
        if package.version != entry['version']:
            raise RuntimeError(f"License version mismatch: {entry['distribution']}")
        if entry['origin'] == 'wheel':
            base = Path(package.locate_file('')).resolve()
            path = Path(package.locate_file(str(source))).resolve()
        elif entry['origin'] == 'pinned-upstream':
            base = root
            path = (root / str(source)).resolve()
        else:
            raise RuntimeError('Unknown license source')
        if not path.is_relative_to(base) or path.name != target.name or not path.is_file():
            raise RuntimeError(f"Pinned license file is missing: {entry['target']}")
        content = path.read_bytes()
        if len(content) != entry['size_bytes'] or hashlib.sha256(content).hexdigest() != entry['sha256']:
            raise RuntimeError(f"Pinned license bytes changed: {entry['target']}")
        checked.append((path, str(target)))
    for name in ('manifest.json', 'README.md'):
        path = root / name
        if not path.is_file():
            raise RuntimeError('License inventory documentation is missing')
        checked.append((path, 'assets/licenses/' + name))
    return checked


def collect_license_data(spec_root):
    return [(str(source), str(PurePosixPath(target).parent))
            for source, target in checked_license_inputs(spec_root)]


def verify_archive(archive, inputs):
    """Verify emitted archive entries against exact bytes of checked inputs."""
    names = {name.replace('\\', '/'): name for name in archive.toc}
    for source, target in inputs:
        name = names.get(target)
        if name is None or archive.extract(name) != source.read_bytes():
            raise RuntimeError(f'Bundled license missing or changed: {target}')
    return len(inputs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    options = parser.parse_args()
    inputs = checked_license_inputs(Path(__file__).resolve().parents[1])
    if options.archive:
        from PyInstaller.archive.readers import CArchiveReader
        count = verify_archive(CArchiveReader(str(options.archive)), inputs)
        print(f'Verified {count} notice/documentation files in backend archive (limited audited scope).')
    else:
        print(f'Verified {len(inputs)} pinned notice/documentation build inputs (limited audited scope).')

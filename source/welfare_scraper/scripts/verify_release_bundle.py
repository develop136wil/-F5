"""Check/package installer bytes, never install or certify runtime behavior.

publish creates a new immutable release directory; existing EXE/ZIP files stay
untouched. Matching archive bytes alone do not prove source or AI correctness.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tomllib
import zipfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_pair(installer, archive):
    installer, archive = Path(installer), Path(archive)
    size = installer.stat().st_size
    if size == 0:
        raise ValueError('Installer is empty')
    expected = digest(installer)
    archive_hash = digest(archive)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) != 1 or entries[0].filename != installer.name:
            raise ValueError('ZIP must contain exactly the named installer at its root')
        entry = entries[0]
        unix_type = stat.S_IFMT(entry.external_attr >> 16) if entry.create_system == 3 else 0
        if (entry.is_dir() or unix_type not in (0, stat.S_IFREG)
                or entry.file_size != size or entry.flag_bits & 1):
            raise ValueError('ZIP installer size/type differs or entry is encrypted')
        # Size is bounded by the supplied installer before decompression; CRC
        # verification also runs when the complete member stream is consumed.
        with package.open(entry) as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('ZIP and standalone installer content differ')
    if (installer.stat().st_size != size or digest(installer) != expected
            or digest(archive) != archive_hash):
        raise ValueError('Installer or ZIP changed during verification')
    return {'installer': installer.name, 'bytes': size, 'sha256': expected,
            'zip_sha256': archive_hash, 'content_identical': True,
            'runtime_verified': False}


def publish_bundle(installer, output_root, *, version):
    installer, output_root = Path(installer).resolve(), Path(output_root).resolve()
    if not installer.is_file() or installer.stat().st_size == 0:
        raise ValueError('A non-empty built installer is required')
    original_hash = digest(installer)
    # The caller supplies a verified SemVer from the project, not a path.
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', version):
        raise ValueError('Invalid release version')
    destination = output_root / f'{version}-{original_hash[:12]}'
    output_root.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)  # Never overwrite an earlier candidate.
    copied = destination / installer.name
    archive = destination / installer.with_suffix('.zip').name
    shutil.copyfile(installer, copied)
    if digest(copied) != original_hash or digest(installer) != original_hash:
        raise ValueError('Installer changed while copying; candidate is incomplete')
    with zipfile.ZipFile(archive, mode='x', compression=zipfile.ZIP_DEFLATED) as package:
        package.write(copied, arcname=copied.name)
    result = verify_pair(copied, archive)
    if digest(installer) != original_hash:
        raise ValueError('Installer changed while packaging; candidate is incomplete')
    result.update(schema_version=1, version=version,
                  verification='archive-content-identity-only',
                  source_build_verified=False)
    # A manifest is the final completion marker. Failed candidates are retained
    # for inspection but cannot appear complete without this file.
    pending = destination / 'manifest.pending'
    with pending.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    # The candidate directory is exclusively created above. Publish only the
    # fully written marker; a serialization/disk error leaves no manifest.json.
    pending.replace(destination / 'manifest.json')
    return destination, result


def project_installer(project_root):
    project = Path(project_root).resolve()
    config = json.loads((project / 'src-tauri/tauri.conf.json').read_text(encoding='utf-8-sig'))
    package = json.loads((project / 'package.json').read_text(encoding='utf-8-sig'))
    cargo = tomllib.loads((project / 'src-tauri/Cargo.toml').read_text(encoding='utf-8-sig'))
    version = config['version']
    if package['version'] != version or cargo['package']['version'] != version:
        raise ValueError('Frontend, native and installer versions differ')
    name = f"{config['productName']}_{version}_x64-setup.exe"
    if Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('Invalid installer filename')
    return project / 'src-tauri/target/release/bundle/nsis' / name, version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='Read-only exact EXE/ZIP comparison')
    check.add_argument('installer', type=Path)
    check.add_argument('archive', type=Path)
    publish = commands.add_parser('publish', help='Copy into a NEW release directory')
    publish.add_argument('--project-root', required=True, type=Path)
    publish.add_argument('--output-root', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            result = verify_pair(args.installer, args.archive)
        else:
            installer, version = project_installer(args.project_root)
            destination, result = publish_bundle(installer, args.output_root, version=version)
            result = {**result, 'directory': str(destination)}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        print(json.dumps({'verified': False, 'error_type': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

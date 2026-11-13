"""Synthetic bytes only: packaging never launches an executable."""
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

_path = Path(__file__).resolve().parents[1] / 'scripts/verify_release_bundle.py'
_spec = importlib.util.spec_from_file_location('release_bundle', _path)
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)


class TestReleaseBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-bundle-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installer = self.root / '복지F5_0.2.14_x64-setup.exe'
        self.installer.write_bytes(b'SYNTHETIC NOT EXECUTABLE\x00version-one')
        self.archive = self.root / 'candidate.zip'

    def zip(self, entries):
        with zipfile.ZipFile(self.archive, 'w') as output:
            for name, value in entries:
                output.writestr(name, value)

    def test_identical_unicode_named_installer_passes_without_runtime_claim(self):
        self.zip([(self.installer.name, self.installer.read_bytes())])
        result = bundle.verify_pair(self.installer, self.archive)
        self.assertTrue(result['content_identical'])
        self.assertFalse(result['runtime_verified'])

    def test_same_size_stale_bytes_are_rejected(self):
        self.zip([(self.installer.name, self.installer.read_bytes().replace(b'one', b'two'))])
        with self.assertRaises(ValueError):
            bundle.verify_pair(self.installer, self.archive)

    def test_installer_changed_during_verification_is_rejected(self):
        self.zip([(self.installer.name, self.installer.read_bytes())])
        original_digest = bundle.digest
        changed = False
        def replace_after_read(path):
            nonlocal changed
            result = original_digest(path)
            if Path(path) == self.installer and not changed:
                changed = True
                self.installer.write_bytes(self.installer.read_bytes().replace(b'one', b'two'))
            return result
        with patch.object(bundle, 'digest', side_effect=replace_after_read):
            with self.assertRaisesRegex(ValueError, 'changed during verification'):
                bundle.verify_pair(self.installer, self.archive)

    def test_archive_changed_during_verification_is_rejected(self):
        self.zip([(self.installer.name, self.installer.read_bytes())])
        original_digest = bundle.digest
        changed = False
        def replace_after_read(path):
            nonlocal changed
            result = original_digest(path)
            if Path(path) == self.archive and not changed:
                changed = True
                # Even a changed archive with identical EXE bytes is no longer
                # the archive whose hash was recorded at verification start.
                with zipfile.ZipFile(self.archive, 'a') as package:
                    package.comment = b'changed during verification'
            return result
        with patch.object(bundle, 'digest', side_effect=replace_after_read):
            with self.assertRaisesRegex(ValueError, 'changed during verification'):
                bundle.verify_pair(self.installer, self.archive)

    def test_symlink_member_is_not_an_installer(self):
        entry = zipfile.ZipInfo(self.installer.name)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.zip([(entry, self.installer.read_bytes())])
        with self.assertRaises(ValueError):
            bundle.verify_pair(self.installer, self.archive)

    def test_wrong_member_size_rejected(self):
        self.zip([(self.installer.name, b'old')])
        with self.assertRaises(ValueError):
            bundle.verify_pair(self.installer, self.archive)

    def test_extra_files_nested_paths_and_wrong_version_are_rejected(self):
        payload = self.installer.read_bytes()
        for entries in [[('../' + self.installer.name, payload)],
                        [('other-version.exe', payload)],
                        [(self.installer.name, payload), ('unexpected.txt', b'x')]]:
            with self.subTest(entries=[name for name, _ in entries]):
                self.zip(entries)
                with self.assertRaises(ValueError):
                    bundle.verify_pair(self.installer, self.archive)

    def test_empty_or_broken_package_is_rejected(self):
        self.archive.write_bytes(b'not a zip')
        with self.assertRaises(zipfile.BadZipFile):
            bundle.verify_pair(self.installer, self.archive)
        self.installer.write_bytes(b'')
        with self.assertRaises(ValueError):
            bundle.verify_pair(self.installer, self.archive)

    def test_publish_is_new_verified_snapshot_and_never_replaces_old_files(self):
        original = self.installer.read_bytes()
        destination, result = bundle.publish_bundle(self.installer, self.root / 'releases', version='0.2.14')
        self.assertEqual(self.installer.read_bytes(), original)
        self.assertEqual((destination / self.installer.name).read_bytes(), original)
        manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['sha256'], result['sha256'])
        self.assertFalse(manifest['source_build_verified'])
        with self.assertRaises(FileExistsError):
            bundle.publish_bundle(self.installer, self.root / 'releases', version='0.2.14')
        self.assertEqual((destination / self.installer.name).read_bytes(), original)

    def test_copy_mutation_does_not_produce_completion_manifest(self):
        def corrupt_copy(_source, destination):
            Path(destination).write_bytes(b'changed')
        with patch.object(bundle.shutil, 'copyfile', side_effect=corrupt_copy):
            with self.assertRaises(ValueError):
                bundle.publish_bundle(self.installer, self.root / 'releases', version='0.2.14')
        self.assertEqual(list((self.root / 'releases').glob('*/manifest.json')), [])
        self.assertIn(b'version-one', self.installer.read_bytes())

    def test_manifest_write_failure_does_not_publish_completion_marker(self):
        def partial_write(_result, stream, **_kwargs):
            stream.write('{')
            raise OSError('synthetic disk failure')
        with patch.object(bundle.json, 'dump', side_effect=partial_write):
            with self.assertRaises(OSError):
                bundle.publish_bundle(self.installer, self.root / 'releases', version='0.2.14')
        self.assertEqual(list((self.root / 'releases').glob('*/manifest.json')), [])
        self.assertEqual(len(list((self.root / 'releases').glob('*/manifest.pending'))), 1)

    def test_path_like_version_rejected_before_creating_output(self):
        with self.assertRaises(ValueError):
            bundle.publish_bundle(self.installer, self.root / 'releases', version='../bad')
        self.assertFalse((self.root / 'releases').exists())

    def test_project_versions_must_match(self):
        native = self.root / 'src-tauri'
        native.mkdir()
        (native / 'tauri.conf.json').write_text(json.dumps({'version':'0.2.14','productName':'복지F5'}), encoding='utf-8')
        (self.root / 'package.json').write_text(json.dumps({'version':'0.2.14'}), encoding='utf-8')
        (native / 'Cargo.toml').write_text('[package]\nversion="0.2.14"\n', encoding='utf-8')
        installer, version = bundle.project_installer(self.root)
        self.assertEqual(installer.name, self.installer.name)
        self.assertEqual(version, '0.2.14')
        (native / 'Cargo.toml').write_text('[package]\nversion="0.2.13"\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            bundle.project_installer(self.root)


if __name__ == '__main__':
    unittest.main()

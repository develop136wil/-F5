"""Pinned notice collection and emitted-byte checks; no app profile or network."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest

from scripts import license_bundle_inputs as notices


ROOT = Path(__file__).resolve().parents[1]


class LicenseBundleInputs(unittest.TestCase):
    def test_installed_pinned_files_and_data_targets_match_inventory(self):
        inputs = notices.checked_license_inputs(ROOT)
        self.assertEqual(len(inputs),9)
        targets = {target for _,target in inputs}
        self.assertTrue(notices.REQUIRED_TARGETS.issubset(targets))
        data = notices.collect_license_data(ROOT)
        self.assertEqual(len(data),len(inputs))
        for (path,target),(source,destination) in zip(inputs,data):
            self.assertEqual(source,str(path))
            self.assertEqual(str(Path(destination) / path.name).replace('\\','/'),target)

    def test_antlr_is_exact_fixed_upstream_file_not_a_reworded_license(self):
        path = ROOT / 'assets/licenses/antlr4-python3-runtime-4.9.3/LICENSE.txt'
        content = path.read_bytes()
        self.assertEqual(len(content),2699)
        self.assertEqual(hashlib.sha256(content).hexdigest(),
            'b1b379fcaf3219593a4c433feb1b35c780bed23fafaae440b1ae2771a9521e3a')

    def test_distribution_version_change_blocks_build(self):
        with self.assertRaisesRegex(RuntimeError,'version mismatch'):
            notices.checked_license_inputs(ROOT,distribution_lookup=lambda _:SimpleNamespace(version='different'))

    def test_missing_changed_or_traversing_input_blocks_build(self):
        for change in ('missing','bytes','inventory','traversal'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                directory = root / 'assets/licenses'
                shutil.copytree(ROOT / 'assets/licenses',directory)
                path = directory / 'antlr4-python3-runtime-4.9.3/LICENSE.txt'
                if change == 'missing': path.unlink()
                elif change == 'bytes': path.write_bytes(path.read_bytes()+b'changed')
                else:
                    manifest_path = directory / 'manifest.json'
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                    if change == 'inventory': manifest['files'].pop()
                    else: manifest['files'][-1]['source']='../../outside/LICENSE.txt'
                    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
                with self.assertRaises(RuntimeError): notices.checked_license_inputs(root)

    def test_archive_checks_actual_file_bytes_and_handles_windows_paths(self):
        inputs = notices.checked_license_inputs(ROOT)
        archived = {target.replace('/','\\'):source.read_bytes() for source,target in inputs}
        archive = SimpleNamespace(toc=archived,extract=archived.__getitem__)
        self.assertEqual(notices.verify_archive(archive,inputs),9)
        target = next(iter(archived))
        original = archived[target]
        archived[target]=original+b'changed'
        with self.assertRaisesRegex(RuntimeError,'missing or changed'): notices.verify_archive(archive,inputs)
        del archived[target]
        with self.assertRaisesRegex(RuntimeError,'missing or changed'): notices.verify_archive(archive,inputs)

    def test_release_spec_calls_checked_collector(self):
        spec=(ROOT / 'api-server.spec').read_text(encoding='utf-8')
        self.assertIn("'scripts' / 'license_bundle_inputs.py'",spec)
        self.assertIn("_document_data += _notice_collector['collect_license_data'](SPECPATH)",spec)

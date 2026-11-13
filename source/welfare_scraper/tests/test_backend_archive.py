"""Synthetic archive entries; no executable or application module is launched."""
import importlib.util
import marshal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_path = Path(__file__).resolve().parents[1] / 'scripts/verify_backend_archive.py'
_spec = importlib.util.spec_from_file_location('backend_archive_verifier', _path)
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)


class TestBackendArchive(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backend-archive-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.executable = self.root / 'api-server.exe'
        self.executable.write_bytes(b'SYNTHETIC NOT EXECUTABLE')
        self.modules = {}
        self.archive_toc = {'PYZ.pyz': ('z',), 'api_server': ('s',)}
        for name in [*verifier.REQUIRED_MODULES, 'api_server']:
            path = self.source / Path(*name.split('.')).with_suffix('.py')
            path.parent.mkdir(parents=True, exist_ok=True)
            # Would fail if executed. Nested code proves filename normalization.
            text = 'raise RuntimeError("must not execute")\ndef nested():\n    return 42\n'
            path.write_text(text, encoding='utf-8')
            self.modules[name] = compile(text, 'another-build-machine.py', 'exec')

    def reader(self, _path):
        test = self
        class Pyz:
            toc = {name: () for name in test.modules if name != 'api_server'}
            def extract(self, name):
                return test.modules[name]
        class Archive:
            toc = test.archive_toc
            def open_embedded_archive(self, name):
                return Pyz()
            def extract(self, name):
                return marshal.dumps(test.modules[name])
        return Archive()

    def verify(self):
        return verifier.verify_archive(self.executable, self.source, reader_factory=self.reader)

    def test_same_code_with_different_filename_passes_without_execution(self):
        result = self.verify()
        self.assertTrue(result['passed'])
        self.assertFalse(result['executable_launched'])
        self.assertFalse(result['runtime_verified'])
        self.assertEqual(len(result['local_modules']), len(verifier.REQUIRED_MODULES) + 1)

    def test_old_backend_code_is_rejected(self):
        self.modules['services.ai_coordinator'] = compile('old = True', 'old.py', 'exec')
        result = self.verify()
        self.assertFalse(result['passed'])
        self.assertEqual(result['mismatched'], ['services.ai_coordinator'])

    def test_missing_required_module_is_rejected(self):
        del self.modules['services.news_workflow_ownership']
        result = self.verify()
        self.assertFalse(result['passed'])
        self.assertEqual(result['missing'], ['services.news_workflow_ownership'])

    def test_stale_entry_point_is_rejected(self):
        self.modules['api_server'] = compile('old = True', 'old.py', 'exec')
        self.assertIn('api_server', self.verify()['mismatched'])

    def test_environment_file_included_in_archive_is_rejected(self):
        self.archive_toc['config\\.env'] = ('x',)
        self.assertFalse(self.verify()['passed'])

    def test_source_changed_during_check_is_rejected(self):
        original = verifier.digest
        def changed(path):
            if Path(path).name == 'utils.py':
                return 'synthetic concurrent change'
            return original(path)
        with patch.object(verifier, 'digest', side_effect=changed):
            self.assertEqual(self.verify()['changed_sources'], ['utils.py'])

    def test_binary_changed_during_check_is_rejected(self):
        original = verifier.digest
        calls = 0
        def changed(path):
            nonlocal calls
            if Path(path) == self.executable:
                calls += 1
                if calls == 2:
                    return 'synthetic concurrent binary change'
            return original(path)
        with patch.object(verifier, 'digest', side_effect=changed):
            self.assertFalse(self.verify()['passed'])

    def test_multiple_archives_invalid_names_and_noncode_entries_are_rejected(self):
        self.archive_toc['extra.pyz'] = ('z',)
        with self.assertRaises(ValueError):
            self.verify()
        del self.archive_toc['extra.pyz']
        self.modules['../unexpected'] = None
        with self.assertRaises(ValueError):
            self.verify()
        del self.modules['../unexpected']
        self.modules['utils'] = None
        with self.assertRaises(ValueError):
            self.verify()

    def test_build_gate_precedes_sidecar_copy(self):
        script = (_path.parents[2] / 'build_app.bat').read_text(encoding='utf-8')
        gate = script.index('scripts\\verify_backend_archive.py')
        copy = script.index('copy /Y')
        self.assertLess(gate, copy)
        self.assertIn('exit /b 1', script[gate:copy])


if __name__ == '__main__':
    unittest.main()

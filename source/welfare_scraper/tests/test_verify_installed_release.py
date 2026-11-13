from pathlib import Path
import tempfile
import unittest

from scripts.verify_installed_release import (
    NSIS_MARKER, RAW_MARKER, desktop_match, verify,
)


class InstalledReleaseTests(unittest.TestCase):
    def test_exact_and_single_bundle_marker(self):
        built = b'MZ-prefix' + RAW_MARKER + b'-payload'
        self.assertEqual(desktop_match(built, built), 'exact')
        self.assertEqual(desktop_match(built.replace(RAW_MARKER, NSIS_MARKER), built),
                         'nsis_marker_only')

    def test_code_changes_truncation_and_extra_bytes_fail(self):
        built = b'MZ-prefix' + RAW_MARKER + b'-payload'
        installed = built.replace(RAW_MARKER, NSIS_MARKER)
        for damaged in (installed[:-1], installed + b'x', installed.replace(b'payload', b'changed')):
            with self.subTest(damaged=damaged):
                self.assertEqual(desktop_match(damaged, built), 'mismatch')

    def test_ambiguous_missing_and_reverse_markers_fail(self):
        for installed, built in (
            (NSIS_MARKER * 2, RAW_MARKER * 2),
            (NSIS_MARKER + NSIS_MARKER, RAW_MARKER + NSIS_MARKER),
            (RAW_MARKER, NSIS_MARKER), (b'other', b'code'), (b'', b''),
        ):
            self.assertEqual(desktop_match(installed, built), 'mismatch')

    def test_files_missing_changed_and_empty_fail_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / 'installed'
            installed.mkdir()
            built = [root / name for name in ('desktop', 'backend', 'icon')]
            names = ['welfare-tauri.exe', 'api-server.exe', 'welfareF5.ico']
            for path, name in zip(built, names):
                path.write_bytes(b'fixture')
                (installed / name).write_bytes(b'fixture')
            self.assertEqual(verify(installed, *built), dict.fromkeys(('desktop','backend','icon'), 'exact'))
            (installed / names[1]).write_bytes(b'changed')
            (installed / names[2]).unlink()
            before = {p.name:p.read_bytes() for p in installed.iterdir()}
            self.assertEqual(verify(installed, *built),
                             {'desktop':'exact','backend':'mismatch','icon':'unreadable'})
            self.assertEqual(before, {p.name:p.read_bytes() for p in installed.iterdir()})
            built[1].write_bytes(b'')
            (installed / names[1]).write_bytes(b'')
            self.assertEqual(verify(installed, *built)['backend'], 'mismatch')


if __name__ == '__main__':
    unittest.main()

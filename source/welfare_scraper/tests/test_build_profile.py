"""빌드 의존성 검사도 운영 프로필을 열지 않도록 보장."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class TestBuildProfile(unittest.TestCase):
    def test_build_subprocess_receives_disposable_profile_and_no_credentials(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'build_backend_isolated.py'
        spec = importlib.util.spec_from_file_location('isolated_build_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        observed = []

        def build(command, cwd, env):
            profile = Path(env['LOCALAPPDATA'])
            observed.append(profile)
            self.assertNotEqual(str(profile), os.environ.get('LOCALAPPDATA'))
            self.assertTrue((profile / 'welfare-scraper' / 'welfare.db').exists())
            self.assertEqual(env['GOOGLE_API_KEY'], '')
            self.assertEqual(env['NOTION_TOKEN'], '')
            self.assertEqual(env['NOTION_POLICY_DBS'], '[]')
            self.assertEqual(env['APPDATA'], str(profile))
            self.assertEqual(env['PYTHON_DOTENV_DISABLED'], '1')
            self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
            for key in ('TEMP', 'TMP', 'PYINSTALLER_CONFIG_DIR'):
                self.assertTrue(Path(env[key]).is_relative_to(profile))
                self.assertTrue(Path(env[key]).is_dir())
            self.assertNotIn('SYNTHETIC_OTHER_API_KEY', env)
            self.assertNotIn('WELFARE_DESKTOP_SESSION', env)
            self.assertIn('api-server.spec', command)
            return type('Result', (), {'returncode': 0})()

        with patch.dict(os.environ, {'SYNTHETIC_OTHER_API_KEY': 'synthetic-never-a-real-key', 'WELFARE_DESKTOP_SESSION': 'synthetic-session'}), \
             patch.object(module.sys, 'argv', ['build', '--distpath', 'test-dist', '--workpath', 'test-work']), \
             patch.object(module.subprocess, 'run', side_effect=build):
            self.assertEqual(module.main(), 0)
        self.assertFalse(observed[0].exists())

"""Run the existing pytest retention contracts under the isolated unittest entry."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class RetentionRegressionSuite(unittest.TestCase):
    def test_existing_retention_contracts(self):
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
            'tests/test_news_record_retention.py'], cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())

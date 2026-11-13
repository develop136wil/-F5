import json
import os
import tempfile
import unittest
from unittest.mock import patch

import utils


class TestAtomicConfigSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, 'config.json')
        self.path_patch = patch.object(utils, 'get_config_path', return_value=self.config_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_save_replaces_file_and_keeps_previous_valid_backup(self):
        utils.save_config_file({'A': 'old'})
        utils.save_config_file({'A': 'new', '한글': '보존'})

        with open(self.config_path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'A': 'new', '한글': '보존'})
        with open(self.config_path + '.bak', encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'A': 'old'})

    def test_replace_failure_is_propagated_and_original_survives(self):
        utils.save_config_file({'A': 'old'})
        real_replace = os.replace

        def fail_only_live_replace(src, dst):
            if dst == self.config_path:
                raise PermissionError('simulated')
            return real_replace(src, dst)

        with patch.object(os, 'replace', side_effect=fail_only_live_replace):
            with self.assertRaises(PermissionError):
                utils.save_config_file({'A': 'new'})

        with open(self.config_path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'A': 'old'})

    def test_environment_changes_only_after_successful_persist(self):
        old = os.environ.get('ATOMIC_CONFIG_TEST')
        try:
            with patch.object(utils, 'save_config_file', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    utils.set_env_values({'ATOMIC_CONFIG_TEST': 'new'})
            self.assertEqual(os.environ.get('ATOMIC_CONFIG_TEST'), old)
        finally:
            if old is None:
                os.environ.pop('ATOMIC_CONFIG_TEST', None)
            else:
                os.environ['ATOMIC_CONFIG_TEST'] = old


if __name__ == '__main__':
    unittest.main()

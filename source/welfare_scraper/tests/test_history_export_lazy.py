"""Deferred export dependency must preserve file contents and error cleanup."""
import ast
import builtins
import csv
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_manager import items


class DeferredExportTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory()
        self.addCleanup(self.profile.cleanup)
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': self.profile.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.row = {'title': '=HYPERLINK("https://example.invalid")',
                    'site_name': '시험 기관', 'start_age_months': 0,
                    'ai_data': {'source_site_name': '공식 시험 자료', 'related_urls': ['https://example.invalid/a']}}

    def test_pandas_is_not_a_module_level_dependency(self):
        tree = ast.parse(Path(items.__file__).read_text(encoding='utf-8'))
        eager = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        for node in eager:
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
            self.assertFalse(any(name.split('.')[0] == 'pandas' for name in names))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'export_data')
        self.assertTrue(any(isinstance(node, ast.Import) and any(alias.name == 'pandas' for alias in node.names) for node in ast.walk(function)))

    def test_csv_retains_bom_columns_values_and_formula_protection(self):
        with patch.object(items, 'search_items', return_value={'items': [self.row]}):
            result = Path(items.export_data())
        self.assertTrue(result.read_bytes().startswith(b'\xef\xbb\xbf'))
        with result.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, [label for _, label in items._EXPORT_COLUMNS])
        self.assertEqual(rows[0]['제목'], "'" + self.row['title'])
        self.assertEqual(rows[0]['수집 사이트'], '시험 기관')
        self.assertEqual(rows[0]['시작 월령(개월)'], '0')
        self.assertEqual(rows[0]['출처 기관'], '공식 시험 자료')

    def test_excel_preserves_values_and_never_writes_title_as_formula(self):
        from openpyxl import load_workbook
        with patch.object(items, 'search_items', return_value={'items': [self.row]}):
            result = items.export_data('excel')
        workbook = load_workbook(result)
        try:
            sheet = workbook.active
            headers = [cell.value for cell in sheet[1]]
            title = sheet.cell(2, headers.index('제목') + 1)
            self.assertEqual(title.value, "'" + self.row['title'])
            self.assertNotEqual(title.data_type, 'f')
            self.assertEqual(sheet.cell(2, headers.index('시작 월령(개월)') + 1).value, 0)
        finally:
            workbook.close()

    def test_empty_csv_keeps_headers(self):
        with patch.object(items, 'search_items', return_value={'items': []}):
            result = items.export_data()
        with open(result, encoding='utf-8-sig', newline='') as source:
            rows = list(csv.reader(source))
        self.assertEqual(rows, [[label for _, label in items._EXPORT_COLUMNS]])

    def test_failed_deferred_import_leaves_no_export_file(self):
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == 'pandas':
                raise ImportError('simulated missing export dependency')
            return original(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=unavailable), patch.object(items, 'search_items') as search:
            self.assertEqual(items.export_data(), '')
            search.assert_not_called()
        self.assertEqual(list(Path(self.profile.name).rglob('*')), [])


if __name__ == '__main__':
    unittest.main()

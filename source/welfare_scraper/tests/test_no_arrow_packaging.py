"""CSV/XLSX remain usable when optional Arrow libraries are not distributed."""
import importlib.abc
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

class NoArrowPackaging(unittest.TestCase):
    def test_address_files_and_excel_writer_without_arrow(self):
        class BlockArrow(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'pyarrow' or fullname.startswith('pyarrow.'):
                    raise ModuleNotFoundError("Arrow intentionally unavailable", name='pyarrow')
        removed = {key:value for key,value in sys.modules.items() if key == 'pyarrow' or key.startswith('pyarrow.')}
        finder = BlockArrow()
        for key in removed:
            del sys.modules[key]
        sys.meta_path.insert(0, finder)
        try:
            import pandas as pd
            from services.site_import import read_address_file
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = pd.DataFrame({'사이트 이름 (선택)': ['복지기관'], '게시판 주소 (필수)': ['https://example.invalid/notices']})
                for encoding in ('utf-8-sig', 'cp949'):
                    path = root / 'sites.csv'
                    rows.to_csv(path, index=False, encoding=encoding)
                    parsed, duplicates = read_address_file(path, '.csv')
                    self.assertEqual(parsed, [{'url':'https://example.invalid/notices','name':'복지기관'}])
                    self.assertEqual(duplicates, 0)
                path = root / 'sites.xlsx'
                rows.to_excel(path, index=False, engine='openpyxl')
                self.assertEqual(read_address_file(path, '.xlsx')[0][0]['name'], '복지기관')
                output = io.BytesIO()
                pd.DataFrame([{'제목':'지원 소식', '내용':'대상과 신청 방법'}]).to_excel(output, index=False, engine='openpyxl')
                self.assertGreater(len(output.getvalue()), 100)
        finally:
            sys.meta_path.remove(finder)
            sys.modules.update(removed)

    def test_spec_excludes_only_optional_arrow_not_document_engines(self):
        spec = (Path(__file__).resolve().parents[1] / 'api-server.spec').read_text(encoding='utf-8')
        import ast
        tree = ast.parse(spec)
        analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Analysis')
        excluded = ast.literal_eval(next(k.value for k in analysis.keywords if k.arg == 'excludes'))
        self.assertIn('pyarrow', excluded)
        for required in ('pandas','numpy','openpyxl','rapidocr','onnxruntime','pypdfium2','olefile'):
            self.assertNotIn(required, excluded)

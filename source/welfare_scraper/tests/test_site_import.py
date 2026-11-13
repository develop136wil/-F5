import io
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd
from fastapi import BackgroundTasks, HTTPException, UploadFile
from services.site_import import HEADERS, parse_addresses, read_address_file


class AddressFileTests(unittest.TestCase):
    def test_korean_optional_name_whitespace_duplicates_and_blank_rows(self):
        rows, skipped = parse_addresses(pd.DataFrame([
            ['도봉구청', ' https://example.org/board '], ['', ''],
            ['', 'https://example.org/other'], ['중복', 'https://example.org/board']], columns=HEADERS))
        self.assertEqual(rows, [{'name':'도봉구청','url':'https://example.org/board'},
                                {'name':'','url':'https://example.org/other'}])
        self.assertEqual(skipped, 1)

    def test_url_only_and_legacy_headers(self):
        for header in ['URL', ' url ', '\ufeff게시판 주소 (필수)', '사이트 주소']:
            rows, _ = parse_addresses(pd.DataFrame({header:['https://example.org']}))
            self.assertEqual(rows[0]['name'], '')
        rows, _ = parse_addresses(pd.DataFrame({'name':['Old'],'url':['https://example.org'],'list_item':['li']}))
        self.assertEqual(rows[0]['name'], 'Old')

    def test_invalid_rows_reject_entire_file_with_row_numbers(self):
        for value in ['', 'not a URL', 'javascript:alert(1)', 'https://', 'https://a.org:abc', 'https://user:pass@a.org']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '3행'):
                parse_addresses(pd.DataFrame({'name':['ok','bad'],'url':['https://example.org',value]}))

    def test_empty_missing_duplicate_headers_and_limits(self):
        for frame in [pd.DataFrame(columns=HEADERS), pd.DataFrame({'name':['x']}),
                      pd.DataFrame({'url':['https://a.org'],'주소':['https://b.org']}),
                      pd.DataFrame({'url':['https://a.org']*5001})]:
            with self.assertRaises(ValueError):
                parse_addresses(frame)

    def test_csv_encodings_and_quoted_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'sites.csv'
            for encoding in ['utf-8-sig','utf-8','cp949']:
                path.write_bytes(('사이트 이름 (선택),게시판 주소 (필수)\r\n"기관, 지점",https://example.org/board\r\n').encode(encoding))
                rows, _ = read_address_file(path, '.csv')
                self.assertEqual(rows[0]['name'], '기관, 지점')

    def test_actual_template_roundtrip_and_instructions_are_not_imported(self):
        template=Path(__file__).parents[1]/'assets/site_import_template.xlsx'
        with self.assertRaisesRegex(ValueError, '등록할 주소가 없습니다'):
            read_address_file(template, '.xlsx')
        # Simulate typing into the actual exported blank template, preserving its styling.
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'filled.xlsx'
            with zipfile.ZipFile(template) as original, zipfile.ZipFile(path,'w') as filled:
                for name in original.namelist():
                    content=original.read(name)
                    if name=='xl/worksheets/sheet1.xml':
                        tree=ET.fromstring(content)
                        row=tree.find("m:sheetData/m:row[@r='2']",ns)
                        for ref,value in [('A2','직접 입력한 이름'),('B2','https://example.org/notices')]:
                            cell=row.find(f"m:c[@r='{ref}']",ns)
                            if cell is None: cell=ET.SubElement(row,'{'+ns['m']+'}c',{'r':ref})
                            cell.clear(); cell.attrib.update(r=ref,t='inlineStr')
                            inline=ET.SubElement(cell,'{'+ns['m']+'}is')
                            ET.SubElement(inline,'{'+ns['m']+'}t').text=value
                        content=ET.tostring(tree)
                    filled.writestr(name,content)
            rows,_=read_address_file(path,'.xlsx')
            self.assertEqual(rows,[{'name':'직접 입력한 이름','url':'https://example.org/notices'}])


class ImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_names_to_shared_registration_and_skips_existing(self):
        import api_server as api
        file=UploadFile(filename='sites.CSV',file=io.BytesIO(('사이트 이름 (선택),게시판 주소 (필수)\n기관,https://a.org\n,https://b.org\n,https://a.org\n').encode()))
        start=AsyncMock(return_value={'status':'started','count':1})
        tasks=BackgroundTasks()
        with patch.object(api.database,'run_db',AsyncMock(return_value=[{'url':'https://b.org'}])), patch.object(api,'_start_site_batch',start):
            result=await api.import_sites(tasks,file)
        self.assertEqual(result['skipped'],2)
        start.assert_awaited_once_with(['https://a.org'],tasks,{'https://a.org':'기관'})

    async def test_invalid_file_never_starts_registration(self):
        import api_server as api
        start=AsyncMock()
        file=UploadFile(filename='sites.csv',file=io.BytesIO(b'url\nhttps://a.org\ninvalid\n'))
        with patch.object(api,'_start_site_batch',start), self.assertRaises(HTTPException) as error:
            await api.import_sites(BackgroundTasks(),file)
        self.assertEqual(error.exception.status_code,400)
        start.assert_not_awaited()

    async def test_existing_addresses_do_not_overwrite_or_start_ai(self):
        import api_server as api
        start=AsyncMock()
        file=UploadFile(filename='sites.csv',file=io.BytesIO(b'url\nhttps://a.org\n'))
        with patch.object(api.database,'run_db',AsyncMock(return_value=[{'url':'https://a.org'}])), patch.object(api,'_start_site_batch',start):
            result=await api.import_sites(BackgroundTasks(),file)
        self.assertEqual(result,{'status':'success','count':0,'skipped':1})
        start.assert_not_awaited()

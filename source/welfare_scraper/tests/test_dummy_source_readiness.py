import unittest
from services import notion_service

class DummySourceReadiness(unittest.TestCase):
    def extract(self, values):
        names=notion_service.get_policy_source_props()
        page={'id':'synthetic','properties':{name:{'type':'url','url':value} for name,value in zip(names,values)}}
        return notion_service.extract_page_fields(page)['_source_urls']

    def test_labelled_address_is_extracted_and_duplicates_removed(self):
        self.assertEqual(self.extract(['사업소개에서 확인\nhttps://example.invalid/policy?id=1',
                                      'https://example.invalid/policy?id=1']),
                         ['https://example.invalid/policy?id=1'])

    def test_missing_protocol_or_menu_text_is_not_guessed(self):
        for value in ['www.example.invalid','사업소개 → 지원사업 클릭','']:
            with self.subTest(value=value):
                self.assertEqual(self.extract([value]),[])

    def test_three_distinct_sources_keep_order(self):
        urls=['https://example.invalid/a','https://example.invalid/b','https://example.invalid/c']
        self.assertEqual(self.extract(urls),urls)

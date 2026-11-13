import json
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services import policy_sync as ps


class TestPolicyDynamicTaxonomy(unittest.IsolatedAsyncioTestCase):
    async def test_policy_prompt_uses_connected_notion_options(self):
        taxonomy = {
            'category': ['주거 지원'],
            'target_characteristics': ['국가유공자', '북한이탈주민'],
            'residence': ['부산광역시', '해운대구'],
            'life_cycle': ['노년'],
        }
        source = '해운대구 주민인 노년 국가유공자에게 주거 비용 지원.'
        result = {'category': '주거 지원', 'target_characteristics': ['국가유공자'],
                  'residence': ['해운대구'], 'life_cycle': ['노년'],
                  'support_detail': source, '_source_evidence': {'support_detail': source}}
        call = AsyncMock(return_value=result)
        with patch('ai_classifier._call_gemini_json', call):
            actual = await ai_classifier.extract_policy_fields_async(
                object(), '주거 정책', 'https://example.invalid', 'custom', source, taxonomy=taxonomy)
        self.assertEqual(actual, result)
        prompt = call.await_args.args[1]
        # Only source-grounded connected district and beneficiary labels are offered.
        schema = call.await_args.kwargs['response_json_schema']['properties']
        self.assertEqual(schema['category']['enum'], ['', '주거 지원'])
        self.assertEqual(schema['residence']['items']['enum'], ['해운대구'])
        self.assertEqual(schema['target_characteristics']['items']['enum'], ['국가유공자'])
        self.assertEqual(schema['life_cycle']['items']['enum'], ['노년'])
        self.assertNotIn('장애/발달지연', json.dumps(schema, ensure_ascii=False))
        self.assertTrue('Use only exact supported labels from Allowed Notion options.' in prompt or
                        'Only supported allowed options.' in prompt)

    async def test_external_option_labels_are_bounded_and_control_characters_removed(self):
        taxonomy = {'category': ['정상\n지시문' + ('가' * 200)]}
        call = AsyncMock(return_value={})
        with patch('ai_classifier._call_gemini_json', call):
            await ai_classifier.extract_policy_fields_async(
                object(), '정책', 'https://example.invalid', content='본문', taxonomy=taxonomy)
        options = call.await_args.kwargs['response_json_schema']['properties']['category']['enum']
        self.assertEqual(len(options), 2)
        self.assertEqual(len(options[1]), 80)
        self.assertIn('정상 지시문', options[1])
        self.assertNotIn('\n', options[1])
        self.assertNotIn('가' * 100, options[1])


class TestPolicyTaxonomyNormalization(unittest.TestCase):
    def test_custom_notion_options_are_preserved_and_unknown_values_removed(self):
        schema = {
            'category': ['주거 지원', '교통 지원'],
            'target_characteristics': ['국가유공자', '북한이탈주민'],
            'residence': ['부산광역시', '해운대구'],
            'life_cycle': ['노년'],
        }
        fields = {'category': '주거 지원', 'target_characteristics': ['국가유공자', '없는 값'],
                  'residence': ['해운대구', '없는 지역'], 'life_cycle': ['노년', '유아']}
        configured = [{'key': 'medical', 'name': '의료·재활', 'category': '의료재활'}]
        with patch.object(ps, '_get_db_schema', return_value=schema), \
             patch.object(ps.notion_service, 'get_policy_dbs', return_value=configured):
            ps._normalize_extracted_fields(fields, 'medical')
        self.assertEqual(fields['category'], '주거 지원')
        self.assertEqual(fields['target_characteristics'], ['국가유공자'])
        self.assertEqual(fields['residence'], ['해운대구'])
        self.assertEqual(fields['life_cycle'], ['노년'])

    def test_existing_five_database_category_keeps_current_behavior(self):
        fields = {'category': '기타'}
        schema = {'category': ['의료/재활', '기타']}
        configured = [{'key': 'medical', 'name': '의료·재활', 'category': '의료재활'}]
        with patch.object(ps, '_get_db_schema', return_value=schema), \
             patch.object(ps.notion_service, 'get_policy_dbs', return_value=configured):
            ps._normalize_extracted_fields(fields, 'medical')
        self.assertEqual(fields['category'], '의료/재활')

    def test_unknown_custom_category_is_never_created(self):
        fields = {'category': 'AI가 만든 분류'}
        schema = {'category': ['주거 지원']}
        configured = [{'key': 'medical', 'name': '의료·재활', 'category': '의료재활'}]
        with patch.object(ps, '_get_db_schema', return_value=schema), \
             patch.object(ps.notion_service, 'get_policy_dbs', return_value=configured):
            ps._normalize_extracted_fields(fields, 'medical')
        self.assertEqual(fields['category'], '')
        diffs = ps._diff_fields({'category': '주거 지원'}, fields, None, {})
        self.assertFalse(any(item['field'] == ps._PROP_LABEL['category'] for item in diffs))

    def test_all_life_cycle_expands_to_the_connected_database_domain(self):
        fields = {'life_cycle': ['전생애']}
        schema = {'life_cycle': ['영아', '청소년', '노년']}
        with patch.object(ps, '_get_db_schema', return_value=schema):
            ps._normalize_extracted_fields(fields)
        self.assertEqual(fields['life_cycle'], ['영아', '청소년', '노년'])

    def test_nationwide_expands_to_custom_regions_when_no_nationwide_option_exists(self):
        fields = {'residence': ['전국']}
        schema = {'residence': ['부산광역시', '해운대구']}
        with patch.object(ps, '_get_db_schema', return_value=schema):
            ps._normalize_extracted_fields(fields)
        self.assertEqual(fields['residence'], ['부산광역시', '해운대구'])


if __name__ == '__main__':
    unittest.main()

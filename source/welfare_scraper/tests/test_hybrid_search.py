import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import embedding_service
from history_manager import embeddings


class TestSearchEmbeddingRecipe(unittest.TestCase):
    def test_recipe_contains_searchable_structured_fields(self):
        text = embedding_service.build_search_embedding_text({
            'title': '노인 맞춤 돌봄',
            'category': '돌봄/양육',
            'target': '만 65세 이상',
            'target_characteristics': ['독거노인', '저소득'],
            'residence': ['도봉구'],
            'life_cycle': ['노년'],
            'eligibility_detail': '기초생활수급자 우선',
            'support_detail': '안전 확인과 생활 지원',
        })
        for value in ('노인 맞춤 돌봄', '독거노인', '도봉구', '노년', '기초생활수급자'):
            self.assertIn(value, text)

    def test_recipe_is_deterministic_and_deduplicates_same_value(self):
        item = {'title': '청소년 지원', 'target': '청소년 지원', 'life_cycle': ['청소년']}
        first = embedding_service.build_search_embedding_text(item)
        self.assertEqual(first, embedding_service.build_search_embedding_text(dict(item)))
        self.assertEqual(first.count('청소년 지원'), 1)


class TestHybridRanking(unittest.TestCase):
    def test_exact_keyword_match_stays_above_semantic_only_match(self):
        lexical = {'items': [{
            'id': 1, 'title': '노인 돌봄 서비스', 'support_detail': '방문 돌봄',
        }], 'total': 1}
        semantic = {'items': [
            {'id': 2, 'title': '아동 돌봄 공지', 'score': 0.92},
            {'id': 1, 'title': '노인 돌봄 서비스', 'score': 0.72},
        ], 'total': 2}
        with patch('history_manager.items.search_items', return_value=lexical), \
             patch.object(embeddings, 'semantic_search', return_value=semantic):
            result = embeddings.hybrid_search('노인 돌봄', [0.1, 0.2], limit=10)
        self.assertEqual(result['items'][0]['id'], 1)
        self.assertEqual(result['items'][0]['match_sources'], ['keyword', 'semantic'])

    def test_keyword_fallback_works_without_query_vector(self):
        lexical = {'items': [{'id': 3, 'title': '한부모 생활 지원'}], 'total': 1}
        with patch('history_manager.items.search_items', return_value=lexical), \
             patch.object(embeddings, 'semantic_search') as semantic_mock:
            result = embeddings.hybrid_search('한부모', None, limit=10)
        self.assertEqual(result['items'][0]['id'], 3)
        self.assertFalse(result['semantic_used'])
        semantic_mock.assert_not_called()

    def test_long_query_supplements_missing_all_terms_with_partial_matches(self):
        exact = {'items': [], 'total': 0}
        partial = {
            '한부모': {'items': [{'id': 7, 'title': '한부모 가족 생활 안정', 'target': '한부모'}]},
            '생활비': {'items': [{'id': 7, 'title': '한부모 가족 생활 안정', 'support_detail': '생활비 지급'}]},
        }
        requested_queries = []

        def fake_search(query=None, **kwargs):
            requested_queries.append(query)
            if query == '한부모 가정 생활비 지원':
                return exact
            return partial.get(query, {'items': [], 'total': 0})

        with patch('history_manager.items.search_items', side_effect=fake_search):
            result = embeddings.hybrid_search('한부모 가정 생활비 지원', None, limit=10)
        self.assertEqual(result['items'][0]['id'], 7)
        self.assertEqual(result['items'][0]['keyword_match'], 'partial_terms')
        self.assertNotIn('가정', requested_queries[1:])


if __name__ == '__main__':
    unittest.main()

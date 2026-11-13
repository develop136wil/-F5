"""Search response presentation only: in-memory DB, no AI or Notion calls."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import api_server
from services import embedding_service


def news_item():
    body = '지원 내용: 청소년에게 교재비 5만원을 지원합니다.\nCOPYRIGHT SYNTHETIC CENTER.'
    return dict(id=1, item_type='news', title='교재비 지원', score=0.91,
        support_detail=body, ai_data=dict(support_detail=body, news_readable=dict(
            version=1, source_sha256=hashlib.sha256(body.encode()).hexdigest(),
            sections=[dict(section='support', quote=body.splitlines()[0])])))


class SearchPresentation(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.connection = patch.object(api_server.database, 'get_connection',
            side_effect=lambda: nullcontext(self.conn))
        self.connection.start()
        self.addCleanup(self.connection.stop)

    def test_projection_preserves_raw_search_data_scores_and_policy_body(self):
        item = news_item()
        policy = dict(id=2, item_type='policy', support_detail='--- [게시글 본문] --- 담당자 정책 원문',
            ai_data={'news_readable':None}, score=0.82)
        results = dict(items=[item, policy],total=2,limit=20,offset=0,mode='hybrid')
        before = deepcopy(results)
        changes = self.conn.total_changes
        shown = api_server._search_display_results(results)
        self.assertEqual(results,before)
        self.assertEqual(shown['items'][0]['support_detail'],item['support_detail'])
        self.assertEqual(shown['items'][0]['score'],0.91)
        self.assertIn('교재비 5만원',shown['items'][0]['news_presentation']['support_detail'])
        self.assertNotIn('COPYRIGHT',shown['items'][0]['news_presentation']['support_detail'])
        self.assertEqual(shown['items'][1],policy)
        for key in ('total','limit','offset','mode'):
            self.assertEqual(shown[key],results[key])
        self.assertEqual(self.conn.total_changes,changes)

    def test_invalid_projection_stays_empty_instead_of_restoring_source(self):
        item = news_item()
        item['ai_data']['news_readable']['source_sha256'] = 'invalid'
        shown = api_server._search_display_results(dict(items=[item],total=1))['items'][0]
        self.assertEqual(shown['news_presentation']['support_detail'],'')
        self.assertIn('원문',shown['news_presentation']['source_note'])
        self.assertEqual(shown['support_detail'],item['support_detail'])

    def test_empty_and_policy_only_results_need_no_extra_connection(self):
        with patch.object(api_server.database,'get_connection',side_effect=AssertionError('No DB needed')):
            for result in [dict(items=[],total=0),dict(items=[dict(item_type='policy',support_detail='정책')],total=1),
                    dict(items=[dict(ai_data={'item_type':'policy'},support_detail='정책')],total=1),
                    dict(items=[dict(ai_data=json.dumps({'item_type':'policy'}),support_detail='정책')],total=1)]:
                self.assertIs(api_server._search_display_results(result),result)

    def test_policy_type_inside_ai_data_is_not_reprojected_in_mixed_results(self):
        for ai in ({'item_type':'policy'},json.dumps({'item_type':'policy'})):
            policy=dict(id=2,ai_data=ai,support_detail='--- [게시글 본문] --- 담당자 정책 원문')
            output=api_server._search_display_results(dict(items=[news_item(),policy],total=2))
            self.assertEqual(output['items'][1],policy)
            self.assertNotIn('news_presentation',output['items'][1])

    def test_keyword_and_semantic_routes_apply_identical_projection_without_ai(self):
        original = dict(items=[news_item()],total=1,limit=20,offset=0)
        before = deepcopy(original)
        client = TestClient(api_server.app)
        with patch.object(api_server.history_manager,'search_items',return_value=original), \
                patch.object(api_server.history_manager,'hybrid_search',return_value=original), \
                patch.object(embedding_service,'get_unavailable_reason',return_value='missing_api_key'), \
                patch.object(embedding_service,'embed_text_async',new=AsyncMock(side_effect=AssertionError('No AI'))) as ai:
            exact = client.get('/api/search',params={'q':'교재비'})
            semantic = client.get('/api/search/semantic',params={'q':'교재비'})
        self.assertEqual(exact.status_code,200,exact.text)
        self.assertEqual(semantic.status_code,200,semantic.text)
        self.assertEqual(exact.json()['items'][0]['news_presentation'],semantic.json()['items'][0]['news_presentation'])
        self.assertIn('교재비 5만원',exact.json()['items'][0]['news_presentation']['support_detail'])
        self.assertNotIn('COPYRIGHT',exact.json()['items'][0]['news_presentation']['support_detail'])
        self.assertEqual(semantic.json()['semantic_unavailable_reason'],'missing_api_key')
        self.assertEqual(original,before)
        ai.assert_not_awaited()

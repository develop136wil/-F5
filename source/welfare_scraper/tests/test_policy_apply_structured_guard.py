"""Offline apply-boundary tests: source proof is required before Notion I/O."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from services import notion_service
from services import policy_sync as ps


SOURCE_URL = 'https://example.invalid/selected'
SCHEMA = {
    'residence': ['전국', '서울시', '도봉구'],
    'life_cycle': ['영아', '유아', '학령기', '청소년', '노년', '전생애'],
    'target_characteristics': ['일반', '한부모', '다문화', '장애/발달지연'],
}


def saved_row(field, value, source=None, *, extracted=None, sources=None,
              url=SOURCE_URL, styled=None, title='합성정책'):
    target = {'field': ps._PROP_LABEL[field], 'old': '기존 정상값',
              'new': value, 'level': 'high', 'snapshot_current': True}
    if styled is not None:
        target['styled'] = styled
    pending = {'fields': [target], 'proposal_schema_version': 2, 'extraction_meta': {
        'prompt_version': ps._EXTRACTION_PROMPT_VERSION},
        'source_diff': {'url': url, 'new': source or '청소년에게 교육비 지원'}}
    return {
        'page_id': 'synthetic-page', 'db_key': 'synthetic', 'title': title,
        'pending_diff': json.dumps(pending, ensure_ascii=False),
        'notion_fields': json.dumps({field: ['기존 정상값']}, ensure_ascii=False),
        'source_texts': json.dumps(sources if sources is not None else
                                  ({SOURCE_URL: source} if source is not None else {}), ensure_ascii=False),
        'last_extracted_fields': json.dumps(extracted, ensure_ascii=False),
    }


class StructuredApplyBoundary(unittest.IsolatedAsyncioTestCase):
    def prepare(self, row, field, *, schema=None, force=True, force_semantic=True):
        original = copy.deepcopy(row)
        with patch.object(ps.database, 'execute_query', return_value=row) as query, \
             patch.object(ps, '_get_db_schema', return_value=schema or SCHEMA), \
             patch.object(notion_service, 'get_page') as read, \
             patch.object(notion_service, 'update_page_properties') as write, \
             patch.object(ps.ai_classifier, 'get_client') as ai:
            result = ps._apply_field_sync(123, ps._PROP_LABEL[field], force, force_semantic)
        self.assertEqual(row, original)
        self.assertEqual(query.call_count, 1)
        self.assertIn('last_extracted_fields', query.call_args.args[0])
        read.assert_not_called()
        write.assert_not_called()
        ai.assert_not_called()
        return result

    def assert_prepared(self, row, field, value, **kwargs):
        prepared, error, _pending = self.prepare(row, field, **kwargs)
        self.assertIsNone(error)
        self.assertEqual(prepared['py_value'], value)
        self.assertEqual(set(prepared['properties']), {ps._PROP_LABEL[field]})
        self.assertEqual(set(prepared['properties'][ps._PROP_LABEL[field]]), {'multi_select'})
        self.assertEqual([item['name'] for item in
                          prepared['properties'][ps._PROP_LABEL[field]]['multi_select']], value)

    def test_explicit_recipient_tags_pass_without_companion_age_or_legacy_cache(self):
        for field, value, source, expected in [
            ('life_cycle', '청소년', '청소년에게 교육비를 지원합니다.', ['청소년']),
            ('life_cycle', '영아, 유아', '지원 대상은 영유아입니다. 돌봄 지원.', ['영아', '유아']),
            ('life_cycle', '노년', '지원 대상\n만 65세 이상 노인\n지원 내용\n돌봄 지원', ['노년']),
            ('target_characteristics', '한부모', '한부모 가구에게 교육비를 지원합니다.', ['일반', '한부모']),
            ('residence', '도봉구', '도봉구에 거주하는 주민에게 문화비 지원.', ['도봉구']),
        ]:
            for extracted in (None, [], {'start_age_months': True, 'end_age_months': 'wrong'}):
                with self.subTest(field=field, extracted=extracted):
                    self.assert_prepared(saved_row(field, value, source, extracted=extracted),
                                         field, expected)

    def test_exact_saved_child_bounds_remain_valid_and_write_only_selected_tag(self):
        for start, end, tags in [(0, 83, ['영아', '유아']), (0, 107, ['영아', '유아', '학령기'])]:
            source = f'지원대상: 생후 {start}~{end}개월 아동. 교육비 지원.'
            for value in (', '.join(tags), '유아'):
                with self.subTest(start=start, end=end, value=value):
                    row = saved_row('life_cycle', value, source,
                                    extracted={'start_age_months': start, 'end_age_months': end,
                                               'life_cycle': ['무관한 과거 태그'], 'contact': '검증하지 않는 무관 필드'})
                    self.assert_prepared(row, 'life_cycle', value.split(', '))

    def test_unverified_or_malformed_child_companions_cannot_widen_support(self):
        source = '지원대상: 생후 0~83개월 아동. 교육비 지원.'
        for bounds in (None, {}, {'start_age_months': 0}, {'end_age_months': 83},
                       {'start_age_months': False, 'end_age_months': 83},
                       {'start_age_months': '0', 'end_age_months': 83},
                       {'start_age_months': 0, 'end_age_months': '83'},
                       {'start_age_months': 36, 'end_age_months': 83},
                       {'start_age_months': 0, 'end_age_months': 107},
                       {'start_age_months': 83, 'end_age_months': 0}):
            with self.subTest(bounds=bounds):
                prepared, error, _ = self.prepare(saved_row('life_cycle', '유아', source,
                                                           extracted=bounds), 'life_cycle')
                self.assertIsNone(prepared)
                self.assertIn('원문 근거', error)

    def test_proxy_exclusion_or_disjoint_bounds_cannot_unlock_child_overlap(self):
        for source in (
            '지원 내용\n교육비 지원\n제공인력\n생후 0~83개월 아동 지원 담당자',
            '지원 제외 대상\n지원 대상\n생후 0~83개월 아동\n지원 내용\n교육비 지원',
            '지원대상: 생후 0~23개월 또는 생후 84~107개월 아동. 교육비 지원.',
        ):
            with self.subTest(source=source):
                prepared, error, _ = self.prepare(saved_row('life_cycle', '유아', source,
                    extracted={'start_age_months': 0, 'end_age_months': 83}), 'life_cycle')
                self.assertIsNone(prepared)
                self.assertIn('원문 근거', error)

    def test_missing_full_source_never_uses_preview_or_unrelated_source(self):
        for sources, url in [
            ({}, SOURCE_URL), ({SOURCE_URL: ''}, SOURCE_URL),
            ({SOURCE_URL: False}, SOURCE_URL),
            ({'https://example.invalid/other': '청소년에게 교육비 지원'}, SOURCE_URL),
            ({'https://example.invalid/a': '청소년에게 교육비 지원',
              'https://example.invalid/b': '청소년에게 교육비 지원'}, None),
        ]:
            for field, value in [('life_cycle', '청소년'), ('residence', '도봉구'),
                                 ('target_characteristics', '한부모')]:
                with self.subTest(sources=sources, field=field):
                    row = saved_row(field, value, sources=sources, url=url)
                    prepared, error, _ = self.prepare(row, field)
                    self.assertIsNone(prepared)
                    self.assertIn('저장된 원문', error)

    def test_sole_legacy_full_source_and_exact_selected_url_remain_usable(self):
        source = '청소년에게 교육비를 지원합니다.'
        self.assert_prepared(saved_row('life_cycle', '청소년', source, url=None),
                             'life_cycle', ['청소년'])
        self.assert_prepared(saved_row('life_cycle', '청소년', source, sources={
            SOURCE_URL: source,
            'https://example.invalid/other': '서비스 제외 대상\n청소년 지원 이용자'}),
            'life_cycle', ['청소년'])

    def test_verified_region_alias_remains_valid_with_exact_connected_options(self):
        row = saved_row('residence', '서울시', '서울특별시에 거주하는 주민에게 문화비 지원.')
        self.assert_prepared(row, 'residence', ['서울시'], schema=dict(SCHEMA, residence=['서울시']))

    def test_empty_selected_section_is_not_evidence(self):
        row = saved_row('life_cycle', '청소년', '청소년에게 교육비 지원')
        with patch.object(ps, '_pick_relevant_snippet', return_value=''):
            prepared, error, _ = self.prepare(row, 'life_cycle')
        self.assertIsNone(prepared)
        self.assertIn('저장된 원문', error)

    def test_final_styled_value_not_original_extract_is_revalidated(self):
        source = '청소년에게 교육비를 지원합니다.'
        row = saved_row('life_cycle', '청소년', source, styled='노년')
        prepared, error, _ = self.prepare(row, 'life_cycle')
        self.assertIsNone(prepared)
        self.assertIn('원문 근거', error)

    def test_all_dropped_trait_tags_cannot_turn_into_general(self):
        for value in ('알수없는태그', '임의범주, 없는선택지', '장애인'):
            with self.subTest(value=value):
                row = saved_row('target_characteristics', value, '장애인에게 교육비 지원')
                prepared, error, _ = self.prepare(row, 'target_characteristics')
                self.assertIsNone(prepared)
                self.assertIn('일반 태그로 대신 바꾸지 않습니다', error)
        self.assert_prepared(saved_row('target_characteristics', '일반', '교육비 지원'),
                             'target_characteristics', ['일반'])

    def test_universal_expansion_requires_proof_for_exact_outgoing_tags(self):
        # Deliberately strict: a universal source token is not substituted for
        # evidence of every locally expanded/custom label at this boundary.
        for field, value, source, schema in [
            ('residence', '전국', '전국민 대상 문화비 지원.', SCHEMA),
            ('life_cycle', '전생애', '모든 연령을 대상으로 문화비 지원.',
             dict(SCHEMA, life_cycle=['영아', '유아', '학령기'])),
        ]:
            with self.subTest(field=field):
                prepared, error, _ = self.prepare(saved_row(field, value, source), field, schema=schema)
                self.assertIsNone(prepared)
                self.assertIn('원문 근거', error)
        self.assert_prepared(saved_row('life_cycle', '전생애', '모든 연령을 대상으로 문화비 지원.'),
                             'life_cycle', ['전생애'])
        self.assert_prepared(saved_row('residence', '전국', '전국민 대상 문화비 지원.'),
                             'residence', ['전국'], schema=dict(SCHEMA, residence=['전국']))

    def test_saved_source_section_is_whole_and_not_an_arbitrary_400_character_crop(self):
        source = ('# 합성정책\n지원 대상\n청소년\n' + '기타 안내 내용\n' * 80 +
                  '지원 제외 대상\n노인에게 다른 서비스 제공\n'
                  '# 다른 정책\n노인에게 문화비 지원')
        self.assert_prepared(saved_row('life_cycle', '청소년', source), 'life_cycle', ['청소년'])
        prepared, error, _ = self.prepare(saved_row('life_cycle', '노년', source), 'life_cycle')
        self.assertIsNone(prepared)
        self.assertIn('원문 근거', error)

    def test_unrelated_bad_tag_does_not_block_non_tag_apply_fields(self):
        for field, value in [('support_detail', '교육비 지원'), ('age_detail', '만 3~5세'),
                             ('target_income', '소득 무관'), ('eligibility_detail', '거주자 지원')]:
            prepared, error, _ = self.prepare(saved_row(field, value), field)
            self.assertIsNone(error)
            self.assertEqual(prepared['py_value'], value)

    async def test_source_hold_is_before_external_read_write_and_persists_no_mutation(self):
        row = saved_row('life_cycle', '청소년', '제출서류\n청소년 특별지원 확인 자료')
        original = copy.deepcopy(row)
        async def run_local(func, *args, **kwargs):
            return func(*args, **kwargs)
        with patch.object(ps.database, 'run_db', new=run_local), \
             patch.object(ps.database, 'execute_query', return_value=row), \
             patch.object(ps.database, 'execute_write') as local_write, \
             patch.object(ps, '_get_db_schema', return_value=SCHEMA), \
             patch.object(notion_service, 'get_page', new=AsyncMock()) as read, \
             patch.object(notion_service, 'update_page_properties', new=AsyncMock()) as write, \
             patch.object(ps.ai_classifier, 'get_client') as ai:
            result = await ps._apply_field_to_notion_locked(123, ps._PROP_LABEL['life_cycle'], True, True)
        self.assertFalse(result['ok'])
        self.assertIn('원문 근거', result['error'])
        read.assert_not_called()
        write.assert_not_called()
        local_write.assert_not_called()
        ai.assert_not_called()
        self.assertEqual(row, original)

    def test_local_validator_failure_is_closed_not_permission_to_apply(self):
        row = saved_row('life_cycle', '청소년', '청소년에게 교육비 지원')
        with patch('services.policy_evidence.structured_policy_issues', side_effect=RuntimeError('synthetic')):
            prepared, error, _ = self.prepare(row, 'life_cycle')
        self.assertIsNone(prepared)
        self.assertIn('보류', error)

    def test_long_source_and_invalid_size_setting_hold_without_regex_or_ai(self):
        for limit, source in [('6000', '청소년에게 교육비 지원\n' + '설명' * 3100),
                              ('12', '청소년에게 교육비를 지원합니다.'),
                              ('invalid', '청소년에게 교육비 지원'),
                              ('0', '청소년에게 교육비 지원'),
                              ('-1', '청소년에게 교육비 지원')]:
            with self.subTest(limit=limit), patch.dict(os.environ, {'POLICY_SOURCE_MAX_CHARS': limit}), \
                 patch('services.policy_evidence.structured_policy_issues') as validate:
                prepared, error, _ = self.prepare(saved_row('life_cycle', '청소년', source), 'life_cycle')
                self.assertIsNone(prepared)
                self.assertIn('검증 길이 한도', error)
                validate.assert_not_called()
        source = '청소년에게 교육비 지원'
        with patch.dict(os.environ, {'POLICY_SOURCE_MAX_CHARS': str(len(source))}):
            self.assert_prepared(saved_row('life_cycle', '청소년', source), 'life_cycle', ['청소년'])

    def test_whole_long_policy_proof_uses_policy_limit_not_news_excerpt_limit(self):
        source = '지원 대상은 청소년입니다. 교육비 지원.\n' + '상세 운영 안내입니다. ' * 650
        self.assertGreater(len(source), 6000)
        with patch.dict(os.environ, {'POLICY_SOURCE_MAX_CHARS': '30000', 'AI_CONTENT_MAX_CHARS': '1500'}):
            self.assert_prepared(saved_row('life_cycle', '청소년', source), 'life_cycle', ['청소년'])

    def test_frozen_actual_accepted_response_cannot_apply_now_even_with_both_forces(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts/ai-release-eval-20260913'
        source_bytes = (root / 'sources/policy-donggu-care.txt').read_bytes()
        ledger_bytes = (root / 'fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
                         '30eb2b5fe803c5e690704cde16953344aaf26c334dbb81ffbfd9da282218742a')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
                         '2fafe741db03ec58e8a6f4e4c5233366c4245d80c0a6198d28cb37079258d647')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        old = next(row for row in rows if row.get('event') == 'case_result'
                   and row.get('case_id') == 'policy-donggu-care')
        self.assertEqual(old['outcome'], 'accepted')
        extracted = old['result']
        row = saved_row('life_cycle', ', '.join(extracted['life_cycle']),
                        source_bytes.decode('utf-8'), extracted=extracted)
        prepared, error, _ = self.prepare(row, 'life_cycle')
        self.assertIsNone(prepared)
        self.assertIn('원문 근거', error)

"""Offline recipient-role regressions; frozen public responses are never edited."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services.policy_evidence import structured_policy_issues, diagnose_policy_evidence


class PolicyRecipientTagContext(unittest.TestCase):
    def test_nonrecipient_mentions_do_not_support_recipient_tags(self):
        cases = [
            ('지원 대상\n돌봄이 필요한 사람\n서비스 제외 대상\n중복 서비스 이용자\n노인맞춤돌봄서비스 지원을 받는 사람', 'life_cycle', '노년'),
            ('지원 대상\n도움이 필요한 사람\n가사 간병 방문지원 서비스 제외 대상\n동일한 돌봄서비스 이용자\n\n노인장기요양보험급여 이용자에게 제공', 'life_cycle', '노년'),
            ('서비스 제공기관 및 제공인력\n제공기관\n등록된 기관\n제공인력\n만 18세 이상으로 노인복지법에 따른 요양보호사 자격취득자', 'life_cycle', '노년'),
            ('제공인력은 노인복지법에 따른 자격을 갖추어야 합니다.', 'life_cycle', '노년'),
            ('노인복지법에 따라 사업을 지원합니다.', 'life_cycle', '노년'),
            ('청소년복지지원법에 따른 사업 지원.', 'life_cycle', '청소년'),
            ('제출서류\n가족관계증명서\n청소년 특별지원 확인 자료\n신청자별 소득 자료', 'life_cycle', '청소년'),
            ('참고자료\n노인 복지 지원 사업 안내', 'life_cycle', '노년'),
            ('※ 자산·소득조사 대상(통합관리팀 시행): 한부모 가족지원, 청소년 특별지원 가정 등 공식 확인된 자료가 있는 신청자의 경우', 'life_cycle', '청소년'),
            ('자료 예시: 한부모 가족지원 확인서', 'target_characteristics', '한부모'),
            ('관련 법령\n한부모가족지원법에 따른 지원 사업', 'target_characteristics', '한부모'),
            ('지원 제외 대상\n청년 가구\n한부모 가구 지원을 받고 있는 사람', 'target_characteristics', '한부모'),
        ]
        for source, field, tag in cases:
            with self.subTest(source=source, tag=tag):
                facts = {field: [tag]}
                saved = copy.deepcopy(facts)
                self.assertIn(field, structured_policy_issues(facts, source))
                self.assertEqual(facts, saved)

    def test_explicit_recipient_evidence_still_passes(self):
        cases = [
            ('청소년에게 교육비를 지원합니다.', {'life_cycle': ['청소년']}),
            ('노인에게 돌봄 서비스를 지원합니다.', {'life_cycle': ['노년']}),
            ('지원 대상\n청소년\n지원 내용\n교육비 지원', {'life_cycle': ['청소년']}),
            ('신청 자격\n어르신\n지원 내용\n돌봄 지원', {'life_cycle': ['노년']}),
            ('한부모 가구에게 교육비를 지원합니다.', {'target_characteristics': ['한부모']}),
            ('청소년에게 교육비 지원. 소득 확인 자료를 제출해야 합니다.', {'life_cycle': ['청소년']}),
            ('제출서류\n노인 복지 지원 자료\n사업개요\n지원 대상\n청소년\n지원 내용\n교육비 지원', {'life_cycle': ['청소년']}),
            ('서비스 제외 대상\n다른 돌봄 지원 이용자\n서비스 지원내용\n노인에게 교통비 지원', {'life_cycle': ['노년']}),
            ('지원 대상\n만 65세 이상인 노인\n제공인력\n만 18세 이상으로 노인복지법상 자격취득자',
             {'life_cycle': ['노년'], 'start_age_months': 780}),
            ('지원대상: 생후 0~107개월 아동. 교육비 지원.',
             {'start_age_months': 0, 'end_age_months': 107, 'life_cycle': ['영아', '유아', '학령기']}),
        ]
        for source, facts in cases:
            with self.subTest(source=source):
                original = copy.deepcopy(facts)
                self.assertEqual(structured_policy_issues(facts, source), [])
                self.assertEqual(facts, original)

    def test_nonrecipient_age_cannot_widen_or_establish_recipient_age(self):
        source = ('지원 대상\n만 65세 미만으로 간병이 필요한 사람\n'
                  '제공인력\n만 18세 이상으로 노인복지법에 따른 요양보호사 자격취득자')
        self.assertEqual(structured_policy_issues({'end_age_months': 779}, source), [])
        self.assertIn('start_age_months', structured_policy_issues({'start_age_months': 216}, source))
        self.assertIn('life_cycle', structured_policy_issues({'end_age_months': 779, 'life_cycle': ['노년']}, source))

    def test_subordinate_target_labels_never_clear_a_nonrecipient_parent(self):
        for heading in ('지원 제외 대상', '서비스 제공기관 및 제공인력', '제출서류'):
            for label in ('대상', '지원 대상', '신청 자격', '연령'):
                with self.subTest(heading=heading, label=label):
                    source = f'{heading}\n{label}\n만 65세 이상 노인\n지원 내용\n문화비 10만원 지원'
                    issues = structured_policy_issues({'start_age_months': 780, 'life_cycle': ['노년']}, source)
                    self.assertIn('start_age_months', issues)
                    self.assertIn('life_cycle', issues)

    def test_literal_lifecycle_cannot_override_disjoint_recipient_age(self):
        for source, facts in [
            ('지원대상: 만 65세 미만 노인에게 간병 지원.', {'end_age_months': 779, 'life_cycle': ['노년']}),
            ('지원대상: 만 65세 미만 노인에게 간병 지원.', {'life_cycle': ['노년']}),
            ('지원대상: 생후 36~83개월 영아에게 교육비 지원.', {'start_age_months': 36, 'end_age_months': 83, 'life_cycle': ['영아']}),
            ('지원대상: 만 65세 이상 전 연령에게 교통비 지원.', {'start_age_months': 780, 'life_cycle': ['전생애']}),
        ]:
            with self.subTest(source=source):
                self.assertIn('life_cycle', structured_policy_issues(facts, source))


class FrozenPublicRecipientRegression(unittest.IsolatedAsyncioTestCase):
    def read_frozen(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts/ai-release-eval-20260913'
        source_bytes = (root / 'sources/policy-donggu-care.txt').read_bytes()
        ledger_bytes = (root / 'fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), '30eb2b5fe803c5e690704cde16953344aaf26c334dbb81ffbfd9da282218742a')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(), '2fafe741db03ec58e8a6f4e4c5233366c4245d80c0a6198d28cb37079258d647')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        result = next(row for row in rows if row.get('event') == 'case_result' and row.get('case_id') == 'policy-donggu-care')
        raw = next(row for row in rows if row.get('event') == 'response' and row.get('case_id') == 'policy-donggu-care')
        return source_bytes.decode('utf-8'), result, json.loads(raw['raw']['text'])

    async def test_closed_response_replays_to_hold_without_changing_response_or_source(self):
        import ai_classifier
        source, old, raw = self.read_frozen()
        raw_before, source_before = copy.deepcopy(raw), source
        self.assertEqual(old['outcome'], 'accepted')  # immutable observation, not a current pass
        self.assertEqual(old['result']['life_cycle'], ['청소년', '노년'])
        self.assertEqual(old['result']['end_age_months'], 779)
        self.assertIn('life_cycle', structured_policy_issues(old['result'], source))
        diagnosis = diagnose_policy_evidence(old['result'], source)
        self.assertFalse(diagnosis['valid'])
        self.assertIn({'code': 'structured_fact_unsupported', 'field': 'life_cycle'}, diagnosis['issues'])
        review = {}
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}), \
                patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=copy.deepcopy(raw))) as call:
            result = await ai_classifier.extract_policy_fields_async(client, '가사간병방문지원사업',
                'https://www.donggu.go.kr/synthetic-replay-only', content=source, review=review)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'structured_evidence_missing')
        self.assertIn('life_cycle', review['fields'])
        self.assertEqual(call.await_count, 1)  # fake transport only; no network or extra repair call
        self.assertEqual(raw, raw_before)
        self.assertEqual(source, source_before)
        self.read_frozen()  # Recheck immutable source/ledger hashes after the replay.


if __name__ == '__main__':
    unittest.main()

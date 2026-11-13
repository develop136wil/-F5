"""Offline regressions distilled from the five authorized synthetic AI responses."""
import unittest
from unittest.mock import AsyncMock, patch
import ai_classifier
from services.policy_evidence import validate_policy_evidence


class LiveFindings(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_states_unknown_and_preserves_quoted_conditions(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={})) as call:
            await ai_classifier.extract_policy_fields_async(object(), '시험', 'https://example.invalid', content='시험 지원 내용')
        prompt = call.await_args.args[1]
        compact = ' '.join(prompt.split())
        if 'Extract ONE policy as schema JSON' in prompt:
            self.assertIn('Unknown contact/category="", tags=[], ages=null.', prompt)
            self.assertIn('paired response schema enums (not evidence)', prompt)
            self.assertIn('Preserve every footnote/exception and condition.', prompt)
            self.assertIn('sorted unique WHOLE IDs, multiple/reused across fields allowed', prompt)
            self.assertIn('no rewritten prose', prompt)
        else:
            self.assertIn('KEEP UNSUPPORTED STRUCTURED VALUES UNKNOWN', prompt)
            self.assertIn('An option is not evidence', compact)
            self.assertIn('Preserve benefit conditions in support_detail too.', prompt)
            self.assertIn('copies the exact original slice into that field AND its evidence', prompt)
            self.assertIn('A unit can appear in multiple fields when their context requires it.', prompt)
            self.assertIn('Never rewrite text, split a unit', prompt)
        self.assertNotIn('few representative items', prompt)
        self.assertNotIn('\\n- 대상자 및 자격 요건', prompt)
        self.assertEqual(call.await_count, 1)

    def test_partial_support_quote_cannot_borrow_other_fields_evidence(self):
        # Distilled from the 0.2.15 authorized live response. No API request.
        amount = '만 65세 이상에게 교통비 월 2만원 지급.'
        terms = '도봉구 거주자만 신청할 수 있다. 본인 명의 계좌가 필요하다.'
        source = amount + ' ' + terms
        result = {
            'support_detail': '✅ 지원 내용\n- ' + amount + '\n📋 신청 자격\n- ' + terms,
            'eligibility_detail': terms,
            'age_detail': '만 65세 이상',
            '_source_evidence': {'support_detail': amount, 'eligibility_detail': terms,
                                 'age_detail': '만 65세 이상'},
        }
        self.assertFalse(validate_policy_evidence(result, source))
        result['_source_evidence']['support_detail'] = source
        self.assertTrue(validate_policy_evidence(result, source))
        result['support_detail'] += '\n- 누구나 신청할 수 있다.'
        self.assertFalse(validate_policy_evidence(result, source))

    def test_placeholder_is_rejected_without_removing_real_source_text(self):
        text = '한부모 가구에 교육비 월 5만원 지원.'
        proposal = '✅ 지원 내용\n- 한부모 가구에 교육비 월 5만원 지원\n📋 신청 자격\n- 대상자 및 자격 요건'
        result = {'support_detail':proposal, '_source_evidence':{'support_detail':text}}
        self.assertFalse(validate_policy_evidence(result,text))
        literal = '기타 안내\n방문 신청 필요'
        self.assertTrue(validate_policy_evidence({'support_detail':literal,
            '_source_evidence':{'support_detail':literal}}, literal))

    def test_unsubstantiated_income_still_fails(self):
        text='교통비 월 2만원 지급'
        self.assertFalse(validate_policy_evidence({'support_detail':text,'target_income':'소득 무관',
            '_source_evidence':{'support_detail':text,'target_income':''}},text))

    def test_dedicated_age_field_does_not_bypass_omission_guard(self):
        text='생후 2개월부터 6개월까지 영아에게 월 10만원 지급.'
        result={'support_detail':'월 10만원 지급','age_detail':'생후 2개월부터 6개월까지',
                '_source_evidence':{'support_detail':text,'age_detail':'생후 2개월부터 6개월까지'}}
        self.assertFalse(validate_policy_evidence(result,text))
        result['support_detail']=text
        self.assertTrue(validate_policy_evidence(result,text))

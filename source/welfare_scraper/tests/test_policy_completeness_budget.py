"""Request accounting on frozen source text; synthetic SDK, not live quality."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
import ai_classifier
from services import ai_coordinator, policy_source_plan as plan


ROOT = Path(__file__).resolve().parents[2] / 'artifacts/ai-holdout-prep-20260913/sources'
SOURCES = {
    'policy-ansan-birth.txt': '6bbc1bf61cec526c6167e28423555048219ab25f22c7f5c923aaaddee3068c5a',
    'policy-goesan-diapers.txt': '03473b6a889c4185d9f7d92f49514eb4e36006203a6c8530884212cfe47ebb48',
}
OPTIONS = {'category': ['복지'], 'target_characteristics':
    ['일반', '장애/발달지연', '장애인', '저소득', '다자녀', '한부모'],
    'residence': ['전국', '괴산군'], 'life_cycle': ['임신/출산', '영아']}


class CompletenessBudget(unittest.IsolatedAsyncioTestCase):
    def sources(self):
        for name, digest in SOURCES.items():
            data = (ROOT / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            yield name, data.decode('utf-8')
            self.assertEqual((ROOT / name).read_bytes(), data)

    async def test_each_expanded_request_is_counted_in_full_and_uses_one_synthetic_call(self):
        for title, source in self.sources():
            with self.subTest(title=title):
                original = copy.deepcopy(OPTIONS)
                prompt, schema = plan.build_policy_extraction_request(source, title,
                    'https://example.invalid/offline', '공개 원문 회귀', OPTIONS,
                    input_token_budget=ai_coordinator.input_token_capacity('gemini-3.5-flash-lite'),
                    defer_token_budget_check=True)
                schema_text = json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
                self.assertLessEqual(len(prompt) + len(schema_text), plan.MAX_POLICY_PROMPT_CHARS)
                self.assertEqual(OPTIONS, original)
                response = types.GenerateContentResponse(candidates=[types.Candidate(
                    content=types.Content(parts=[types.Part(text='null')]), finish_reason='STOP')])
                generate = AsyncMock(return_value=response)
                client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
                review = {}
                with patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as admit:
                    output = await ai_classifier.extract_policy_fields_async(client, title,
                        'https://example.invalid/offline', site_name='공개 원문 회귀',
                        content=source, taxonomy=OPTIONS, review=review)
                self.assertIsNone(output)
                self.assertEqual(review['code'], 'model_abstained')
                generate.assert_awaited_once()
                admit.assert_awaited_once()
                self.assertEqual(generate.await_args.kwargs['contents'], prompt)
                self.assertEqual(admit.await_args.kwargs['input_tokens'],
                    ai_coordinator.estimate_input_tokens([prompt, schema_text]))
                serialized = json.loads(generate.await_args.kwargs['config'].model_dump_json())
                self.assertEqual(serialized['response_json_schema'], schema)

    def test_expanded_requests_at_size_boundary_are_not_silently_truncated(self):
        for title, source in self.sources():
            with self.subTest(title=title):
                args = (source, title, 'https://example.invalid/offline', '공개 원문 회귀', OPTIONS)
                prompt, schema = plan.build_policy_extraction_request(*args)
                size = len(prompt) + len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')))
                with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', size):
                    self.assertEqual(plan.build_policy_extraction_request(*args), (prompt, schema))
                with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', size - 1):
                    with self.assertRaises(plan.PolicySourcePlanError) as caught:
                        plan.build_policy_extraction_request(*args)
                    self.assertEqual(caught.exception.code, 'source_structure_too_large')


if __name__ == '__main__':
    unittest.main()

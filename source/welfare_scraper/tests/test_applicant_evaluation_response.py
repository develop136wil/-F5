import unittest
from types import SimpleNamespace
from scripts.evaluate_applicant_bounded import (
    parse_response, response_summary, EmptyResponse, InvalidResponseJson, NonObjectResponse)


class TestEvaluationResponse(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(parse_response(SimpleNamespace(text='{"ok": true}')), {'ok': True})

    def test_empty_non_json_and_wrong_shape(self):
        for value in (None, '', ' ', 3, 'not json', 'null', '[]', 'true'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_response(SimpleNamespace(text=value))

    def test_errors_are_distinguishable_without_response_text(self):
        for value, error in ((None, EmptyResponse), ('not json', InvalidResponseJson), ('[]', NonObjectResponse)):
            with self.subTest(value=value), self.assertRaises(error):
                parse_response(SimpleNamespace(text=value))

    def test_summary_omits_raw_text_and_unknown_reason(self):
        response = SimpleNamespace(text='private example', candidates=[
            SimpleNamespace(finish_reason='MAX_TOKENS'), SimpleNamespace(finish_reason='private error')])
        summary = response_summary(response)
        self.assertEqual(summary['finish_reasons'], ['MAX_TOKENS', 'UNKNOWN'])
        self.assertEqual(summary['text_length'], 15)
        self.assertNotIn('private', str(summary))

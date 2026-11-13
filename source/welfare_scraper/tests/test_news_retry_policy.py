import unittest
from services.news_retry_policy import decide_retry


class TestNewsRetryPolicy(unittest.TestCase):
    def test_incomplete_response_is_not_an_automatic_retry(self):
        from services.ai_generation_safety import AiResponseReviewRequired
        for reason in ('output_limit', 'blocked', 'empty', 'incomplete'):
            result = decide_retry(AiResponseReviewRequired(reason), attempts=1)
            self.assertEqual(result.reason, 'ai_response_review_required')
            self.assertTrue(result.terminal)
            self.assertEqual(result.delay, 0)

    def test_auth_and_daily_are_not_automatically_retried(self):
        for text, expected in [('401 invalid API key', 'authentication_required'),
                               ('429 quota requests per day', 'daily_quota_exhausted')]:
            result = decide_retry(RuntimeError(text), attempts=1)
            self.assertTrue(result.terminal)
            self.assertEqual(result.reason, expected)

    def test_provider_delay_is_respected(self):
        result = decide_retry(RuntimeError('429 retryDelay: 120s'), attempts=1)
        self.assertEqual(result.reason, 'rate_limited')
        self.assertEqual(result.delay, 122)

    def test_backoff_and_limit(self):
        self.assertEqual(decide_retry(TimeoutError(), attempts=1).delay, 60)
        self.assertEqual(decide_retry(TimeoutError(), attempts=3).delay, 240)
        result = decide_retry(TimeoutError(), attempts=5)
        self.assertTrue(result.terminal)
        self.assertEqual(result.reason, 'retry_limit_reached')

    def test_structured_status_code(self):
        error = RuntimeError('request rejected')
        error.code = 400
        self.assertEqual(decide_retry(error, attempts=1).reason, 'invalid_request')
        error.code = 503
        self.assertEqual(decide_retry(error, attempts=1).reason, 'service_unavailable')

    def test_unknown_message_is_not_persisted(self):
        result = decide_retry(RuntimeError('secret request content'), attempts=1)
        self.assertEqual(result.reason, 'analysis_error')

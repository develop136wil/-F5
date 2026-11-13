"""Preserved public convenience corpus, real admission, fake external SDK only.

No live fetch, OCR, provider quality estimate or new artifact writes. Run only
through the isolated runner; summaries contain lengths/codes, not source text.
"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import ai_classifier
from services import ai_coordinator as quota, news_analysis_store as jobs
from services.news_analysis_worker import process_job
from tests import test_public_request_budget as public


ARTIFACTS = Path(__file__).resolve().parents[2] / 'artifacts'
MANIFESTS = (
    ARTIFACTS / 'public-document-eval-20260909/source-manifest.json',
    ARTIFACTS / 'public-document-eval-20260909/source-manifest-extra.json',
    ARTIFACTS / 'ai-holdout-prep-20260913/sources-manifest.json',
)


def sources(kind):
    for path in MANIFESTS:
        for original in json.loads(path.read_text(encoding='utf-8'))['cases']:
            if original['kind'] != kind:
                continue
            raw = (path.parent / original['source_file']).read_bytes()
            source = raw.decode('utf-8')
            if path.parent.name == 'public-document-eval-20260909':
                # This legacy manifest fingerprints the original read_text()
                # snapshot (universal-newline LF), not checkout CRLF bytes.
                # Replay precisely that verified snapshot; never edit the file
                # or relax its hash check. The newer holdout uses raw bytes.
                source = source.replace('\r\n', '\n').replace('\r', '\n')
            if hashlib.sha256(source.encode('utf-8')).hexdigest() != original['source_sha256']:
                raise AssertionError('Preserved public fixture hash changed')
            case = dict(original)
            case['url'] = original.get('final_url') or original.get('url') or original.get('source_url')
            yield case, source


class CurrentPublicCorpusAdmission(unittest.IsolatedAsyncioTestCase):
    async def measure(self, case, source, *, draft=None):
        fixture = public.PublicRequestAdmission(
            'test_public_news_701_reaches_sdk_full_source_and_incomplete_attachment_stays_review')
        fixture.setUp()
        estimates = []
        estimate = quota.estimate_generation_input_tokens
        def measured(*args, **kwargs):
            value = estimate(*args, **kwargs)
            estimates.append(value)
            return value
        try:
            self.assertEqual(quota.input_token_budget(fixture.model), 6000)
            with patch.object(quota, 'estimate_generation_input_tokens', side_effect=measured), \
                    patch.dict(os.environ, {'CLASSIFY_MODEL': fixture.model}):
                if draft is None:
                    fixture.sdk_reply(fixture.source_preservation_reply())
                    job = fixture.enqueue_public(case, source)
                    state = await process_job(job, fixture.client, call_db=fixture.call_db)
                    saved = jobs.get_job(fixture.conn, job)
                    self.assertEqual(json.loads(saved['input_json'])['content'], source)
                    reason = saved['reason']
                else:
                    fixture.sdk_reply(None)  # Admission only, never an invented fact response.
                    review = {}
                    result = await ai_classifier.extract_policy_fields_async(fixture.client,
                        case['title'], case['url'], site_name=urlsplit(case['url']).hostname,
                        content=source, review=review, draft_preview=draft)
                    self.assertIsNone(result)
                    state, reason = 'review', review.get('code')
            calls = fixture.provider.await_count
            self.assertLessEqual(calls, 1)
            self.assertEqual(quota.status()['recorded_requests_today'], calls)
            if calls:
                self.assertLessEqual(estimates[-1], 6000)
            return dict(id=case['id'], kind=case['kind'], chars=len(source),
                utf8_bytes=len(source.encode('utf-8')), draft=draft, state=state, reason=reason,
                request_units=estimates[-1] if estimates else None, sdk_mock_calls=calls,
                captured_page_status=(case.get('source_page') or {}).get('status', 'unknown'))
        finally:
            fixture.doCleanups()

    async def test_six_public_news_keep_original_page_metadata_and_bounded_admission(self):
        rows = [await self.measure(case, source) for case, source in sources('news')]
        self.assertEqual(len(rows), 6)
        by_id = {row['id']: row for row in rows}
        self.assertEqual(by_id['news-science-terrarium']['sdk_mock_calls'], 1)
        self.assertEqual(by_id['news-memorial-school']['sdk_mock_calls'], 0)
        self.assertEqual(by_id['news-memorial-school']['reason'], 'ai_input_size_review_required')
        self.assertEqual(by_id['news-safety']['sdk_mock_calls'], 0)
        self.assertEqual(by_id['news-safety']['reason'], 'ai_input_review_required')
        print('PUBLIC_CORPUS_NEWS ' + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))

    async def test_five_public_policies_default_taxonomy_draft_and_check_admission(self):
        cases = list(sources('policy'))
        self.assertEqual(len(cases), 5)
        rows = [await self.measure(case, source, draft=draft)
                for case, source in cases for draft in (True, False)]
        for row in rows:
            if row['id'] == 'policy-goesan-diapers':
                self.assertEqual(row['sdk_mock_calls'], 1)
                self.assertEqual(row['reason'], 'model_abstained')
        print('PUBLIC_CORPUS_POLICY ' + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))

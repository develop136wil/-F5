"""Two reviewed public sources; opt-in Gemini, no retries or operational writes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'welfare_scraper'))
EXPECTED = {'family-walking': ('individual', 'proxy'), 'child-health': ('organization',)}


class EmptyResponse(ValueError):
    pass


class InvalidResponseJson(ValueError):
    pass


class NonObjectResponse(ValueError):
    pass


def response_summary(response):
    # Never log generated text, raw HTTP errors, or credentials for diagnosis.
    candidates = getattr(response, 'candidates', None) or []
    known = {'STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER',
             'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'MALFORMED_FUNCTION_CALL'}
    reasons = []
    for candidate in candidates:
        reason = getattr(candidate, 'finish_reason', None)
        name = getattr(reason, 'name', reason)
        reasons.append(name if isinstance(name, str) and name in known else 'UNKNOWN')
    text = response.text
    return {'candidate_count': len(candidates), 'finish_reasons': reasons,
            'text_present': isinstance(text, str) and bool(text.strip()),
            'text_length': len(text) if isinstance(text, str) else 0}


def parse_response(response):
    text = response.text
    if not isinstance(text, str) or not text.strip():
        raise EmptyResponse('Missing JSON response text')
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise InvalidResponseJson('Invalid JSON response') from None
    if not isinstance(result, dict):
        raise NonObjectResponse('JSON response must be an object')
    return result


def prepare(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('state') != 'complete':
        raise ValueError('Source extraction incomplete')
    samples = data['samples']
    if len(samples) != 2 or {row['sample'] for row in samples} != set(EXPECTED):
        raise ValueError('Exactly the reviewed two sources are required')
    for row in samples:
        if not row['url'].startswith('https://health.dobong.go.kr/bbs.asp?') or not row['source']:
            raise ValueError('Unexpected source')
    return samples


async def run(args, rows, report):
    from google import genai
    from google.genai import types
    from services.news_profile import NewsProfile
    from services import news_custom_analysis as custom
    from services.news_target_rules import evaluate_target
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    key = config.get('GOOGLE_API_KEY')
    # Match the application's current default; never switch models after failure.
    model = config.get('CLASSIFY_MODEL') or 'gemini-3.5-flash-lite'
    if not key:
        raise ValueError('Saved key required')
    report['model'], report['recipe'] = model, custom.RECIPE
    client = genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=45000, retry_options=types.HttpRetryOptions(attempts=1)))
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    async def single_call(_client, prompt, title, *, model_override=None, response_json_schema=None):
        if report['api_calls'] >= report['max_api_calls'] or model_override != model:
            raise ValueError('Budget/model mismatch')
        report['api_calls'] += 1
        report['stage'] = 'request_dispatch'
        save()  # An uncertain request remains counted. Never silently repeat it.
        response = await client.aio.models.generate_content(model=model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json',
                response_json_schema=response_json_schema, temperature=0))
        report['stage'] = 'response_parse'
        report['response_summary'] = response_summary(response)
        save()
        result = parse_response(response)
        report['stage'] = 'analysis_postprocessing'
        save()
        return result
    try:
        for index, row in enumerate(rows[:report['max_api_calls']]):
            if index:
                await asyncio.sleep(15)
            profile = NewsProfile('audit', 1, '아동 원문 검증', '서울특별시 도봉구', life_cycles=('학령기',))
            payload = dict(title=row['title'], link=row['url'], site_name='도봉구 보건소',
                           content=row['source'], source_page=row.get('source_page'))
            with patch('ai_classifier._call_gemini_json', new=single_call):
                result = await custom.classify(client, profile, payload, model, complete_grounding_rules=True)
            report['stage'] = 'fact_validation'
            save()
            custom.validate_facts(result)
            # Keep public-source model facts for offline replay of improved rules.
            # Grounding returns a separate mapping; never overwrite the model record.
            raw_analysis = dict(result)
            from services.news_analysis_safety import assess
            checked = assess(profile, result, payload['content'])
            result = checked['analysis']
            status = checked['target_checks']['applicant']
            report['results'].append(dict(sample=row['sample'], expected=list(EXPECTED[row['sample']]),
                role_matches_reference=result['applicant_type'] in EXPECTED[row['sample']],
                applicant_status=status, target_status=checked['target_status'],
                analysis=result, raw_analysis=raw_analysis,
                attachment_status=row['attachments']['status']))
            save()
        report['state'] = 'complete'
    except Exception as error:
        report.update(state='stopped_on_error', error_type=type(error).__name__)
        # Class and numeric provider status only: never save raw error text/keys.
        code = getattr(error, 'code', None)
        if type(code) is int:
            report['status_code'] = code
    finally:
        await client.aio.aclose()
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--max-calls', type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.execute and not args.config:
        parser.error('--execute requires an authorized saved config')
    rows = prepare(args.source)
    report = dict(state='prepared', api_calls=0, max_api_calls=args.max_calls, results=[],
        source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
        independent_labels=False, production_approved=False)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output)
    if args.execute:
        with tempfile.TemporaryDirectory(prefix='welfare-applicant-eval-') as isolated:
            data = Path(isolated) / 'welfare-scraper'
            data.mkdir()
            sqlite3.connect(data / 'welfare.db').close()
            (data / 'config.json').write_text('{}', encoding='utf-8')
            os.environ['LOCALAPPDATA'] = isolated
            try:
                asyncio.run(run(args, rows, report))
            finally:
                if 'database' in sys.modules:
                    sys.modules['database'].close_all_connections()
                import logging
                logging.shutdown()
    print(json.dumps({key: report[key] for key in ('state', 'api_calls', 'production_approved')}))
    return 0 if report['state'] in ('prepared', 'complete') else 1


if __name__ == '__main__':
    sys.exit(main())

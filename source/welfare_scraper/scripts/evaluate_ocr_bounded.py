"""Two synthetic PNGs; default fixture-only, explicit opt-in for live OCR."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import evaluate_finalist_bounded as guard

ROOT = guard.WORKSPACE / 'artifacts' / 'ocr-synthetic-0216'
CASES = [
    {'id': 'senior', 'lines': ['합성 시험 문서 - 실제 지원사업이 아닙니다',
        '노인 교통비 지원 안내', '대상: 만 65세 이상 도봉구 주민',
        '지원: 교통비 월 2만원 지급', '조건: 본인 명의 계좌 필요',
        '제외: 같은 목적의 교통비와 중복 지원 불가',
        '신청: 주민센터 방문 신청', '마감: 2026년 10월 31일'],
     'ages': [780, None], 'deadline': '2026-10-31', 'residence': ['도봉구'], 'target_income': '',
     'facts': ['2만원', '본인', '계좌', '중복', '불가', '방문']},
    {'id': 'infant', 'lines': ['합성 시험 문서 - 실제 지원사업이 아닙니다',
        '영아 보육료 지원 안내', '대상: 생후 2개월부터 6개월까지 영아',
        '지원: 월 10만원 지급', '조건: 보호자가 대신 방문 신청',
        '제외: 다른 보육료와 중복 지원 불가',
        '마감: 2026년 11월 15일'],
     'ages': [2, 6], 'deadline': '2026-11-15', 'residence': [], 'target_income': '',
     'facts': ['10만원', '보호자', '방문', '중복', '불가']},
]


def check_result(case, result):
    prose = ''.join(str(result.get(k) or '') for k in (
        'support_detail', 'eligibility_detail', 'age_detail', 'target'))
    checks = {'valid': not result.get('error') and result.get('is_relevant') is True,
        'ages': [result.get('start_age_months'), result.get('end_age_months')] == case['ages'],
        'deadline': result.get('deadline') == case['deadline'],
        'residence': result.get('residence') == case['residence'],
        'target_income': result.get('target_income') == case['target_income'],
        'contact': result.get('contact') == '', 'related_urls': result.get('related_urls') == []}
    checks.update({fact: fact in prose for fact in case['facts']})
    return checks


def fixtures():
    from PIL import Image, ImageDraw, ImageFont
    ROOT.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 34)
    for case in CASES:
        path = ROOT / (case['id'] + '.png')
        if path.exists():
            continue  # Never overwrite the reviewed source image.
        picture = Image.new('RGB', (1500, 900), 'white')
        draw = ImageDraw.Draw(picture)
        for index, line in enumerate(case['lines']):
            draw.text((55, 45 + index * 95), line, font=font, fill='black')
        picture.save(path)


async def run(key, ledger):
    from google import genai
    from google.genai import types
    from services import ocr_service, ai_coordinator
    client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
        base_url='https://generativelanguage.googleapis.com', timeout=45000,
        retry_options=types.HttpRetryOptions(attempts=1)))
    attempts, completed, current = 0, 0, None
    async def generate(**kwargs):
        nonlocal attempts
        if attempts >= 2 or kwargs['model'] != guard.MODEL:
            raise guard.EvaluationStopped('BudgetClosed')
        if attempts:
            await asyncio.sleep(30)
        data = kwargs['contents'][1].inline_data.data
        if not isinstance(data, bytes) or len(data) > 1024 * 1024:
            raise guard.EvaluationStopped('ImageLimit')
        kwargs['config'].max_output_tokens = 3072
        attempts += 1
        ledger.append({'event': 'attempt_reserved', 'attempt': attempts,
            'case_id': current['id'], 'sha256': hashlib.sha256(data).hexdigest()})
        try:
            response = await asyncio.wait_for(client.aio.models.generate_content(**kwargs), 45)
            ledger.append({'event': 'response', 'case_id': current['id'],
                           'raw': guard.generation_record(response)})
            return response
        except BaseException as error:
            ledger.append({'event': 'request_failed', 'error': guard.safe_error(error)})
            raise guard.EvaluationStopped('RequestFailed') from None
    async def admission(*args, **kwargs):
        return None
    async def direct(value, **kwargs):
        return await value
    proxy = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    try:
        with patch.object(ai_coordinator, 'acquire_model_slot', admission), \
                patch.object(ai_coordinator, 'await_api', direct), \
                patch.dict(os.environ, OCR_MODEL=guard.MODEL):
            for current in CASES:
                data = (ROOT / (current['id'] + '.png')).read_bytes()
                result = await ocr_service._analyze_document_bytes(proxy, data)
                checks = check_result(current, result)
                ledger.append({'event': 'case_result', 'case_id': current['id'],
                    'result': result, 'checks': checks, 'passed': all(checks.values())})
                if not all(checks.values()):
                    raise guard.EvaluationStopped('QualityFailed')
                completed += 1
        result = {'status': 'complete', 'attempts': attempts, 'completed': completed}
    except BaseException as error:
        result = {'status': 'stopped', 'attempts': attempts, 'completed': completed,
                  'error': guard.safe_error(error)}
    finally:
        await client.aio.aclose()
        client.close()
    ledger.append({'event': 'summary', **result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--free-tier-confirmed', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--run-name', default='live-once',
                        choices=('live-once', 'required-fields-recheck', 'missing-grounding-recheck'))
    args = parser.parse_args()
    if not args.execute:
        fixtures()
        print('Fixture-only: two PNGs prepared; API requests=0')
        return 0
    if not args.free_tier_confirmed or not args.config:
        return 2
    with guard.single_run_lock():
        output = ROOT / args.run_name
        output.mkdir(exist_ok=False)  # No accidental repeat, even after failure.
        key = json.loads(args.config.read_text(encoding='utf-8-sig')).get('GOOGLE_API_KEY')
        if not isinstance(key, str) or not key:
            return 2
        ledger = guard.Ledger(output / 'ledger.jsonl', secret=key)
        try:
            ledger.append({'event': 'plan', 'max_requests': 2, 'model': guard.MODEL,
                'retry_attempts': 1, 'min_interval': 30, 'cases': CASES,
                'limits': 'Clean synthetic PNG only, not degraded scans/PDF or production admission.'})
            with guard.isolated_runtime(output):
                result = asyncio.run(run(key, ledger))
            print(json.dumps(result))
            return 0 if result['status'] == 'complete' else 1
        finally:
            ledger.close()


if __name__ == '__main__':
    raise SystemExit(main())

"""Two public samples, optional four-call Gemini comparison, isolated app profile.

No Notion/operational DB writes, no retries or model switching. Saved reports
contain public source text and model answers, never API keys/provider errors.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'welfare_scraper'))
PAGES = [
    ('family-walking', 'https://health.dobong.go.kr/bbs.asp?bmode=D&code=10005006&pcode=12743911'),
    ('child-health', 'https://health.dobong.go.kr/bbs.asp?bmode=D&code=10005006&pcode=12742620'),
]


async def run(args):
    import aiohttp
    import ai_classifier
    from services import news_attachments, news_ai_input, ai_coordinator
    from google.genai import types
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.ai else {}
    key = config.get('GOOGLE_API_KEY', '')
    model = config.get('CLASSIFY_MODEL') or os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
    if args.ai and not key:
        raise ValueError('Configured API key required')
    report = {'model': model, 'api_calls': 0, 'samples': [], 'state': 'running',
              'scope': 'public text/local attachments only; no OCR; not an accuracy benchmark'}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)

    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    client = ai_coordinator.create_client(key) if args.ai else None

    async def single_call(_client, prompt, title, *, model_override=None):
        if report['api_calls'] >= (2 if args.replay else 4):
            raise RuntimeError('Bounded call budget exhausted')
        await ai_coordinator.acquire_model_slot(model)
        report['api_calls'] += 1
        save()  # Persist attempts before dispatch; never silently retry uncertain calls.
        response = await ai_coordinator.await_api(client.aio.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json', temperature=0)))
        return json.loads(response.text)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            replay_rows = json.loads(Path(args.replay).read_text(encoding='utf-8'))['samples'] if args.replay else None
            for index, (slug, url) in enumerate(PAGES):
                if args.sample and slug != args.sample:
                    continue
                if replay_rows:
                    old = replay_rows[index]
                    if old['url'] != url:
                        raise ValueError('Replay source does not match public sample')
                    source = old['source']
                    page = {'title': old['title'], 'source_page': old['source_page']}
                    attachments = old['attachments']
                else:
                    page = await ai_classifier.fetch_page_content_async(session, url)
                    if not page.get('text'):
                        raise ConnectionError('Public sample unavailable')
                    page = await news_attachments.enrich_page_locally(session, page)
                    source = news_attachments.merge_page_text(page, include_notices=False)
                    attachments = news_attachments.summarize_attachment_reports(page.get('attachment_reports'))
                selected, coverage = news_ai_input.prepare(source, 6000)
                row = {'sample': slug, 'url': url, 'title': page['title'], 'source': source,
                       'selected': selected, 'coverage': coverage, 'source_page': page.get('source_page'),
                       'attachments': attachments,
                       'same_input_as_prefix': selected == source[:6000], 'results': {}}
                report['samples'].append(row)
                save()
                print(json.dumps({'sample': slug, 'chars': len(source), 'same_input': row['same_input_as_prefix']}, ensure_ascii=False), flush=True)
                if args.ai:
                    with patch.object(ai_classifier, '_call_gemini_json', new=single_call):
                        for strategy in (('balanced',) if args.replay else ('prefix', 'balanced')):
                            row['results'][strategy] = await ai_classifier.classify_post_async(
                                client, title=page['title'], link=url, site_name='도봉구 보건소',
                                content=source, custom_prompt='', strict=True, model_override=model,
                                content_max_chars=6000, input_strategy=strategy,
                                source_page=page.get('source_page') if strategy == 'balanced' else None)
                            save()
                            print(json.dumps({'sample': slug, 'strategy': strategy, 'state': 'complete'}), flush=True)
        report['state'] = 'complete'
    except Exception as error:
        report['state'] = 'stopped'
        report['error_type'] = type(error).__name__
        print(json.dumps({'state': 'stopped', 'error_type': type(error).__name__}), flush=True)
    finally:
        if client:
            await client.aio.aclose()
        save()
    return 0 if report['state'] == 'complete' else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ai', action='store_true')
    parser.add_argument('--config')
    parser.add_argument('--output', required=True)
    parser.add_argument('--replay', help='Reuse the two previously saved public sources; at most two calls')
    parser.add_argument('--sample', choices=[name for name, _ in PAGES])
    args = parser.parse_args()
    if args.ai and not args.config:
        parser.error('--ai requires --config')
    if Path(args.output).exists():
        parser.error('Choose a new report path; existing attempts must not be overwritten')
    with tempfile.TemporaryDirectory(prefix='welfare-quality-') as profile:
        data = Path(profile) / 'welfare-scraper'
        data.mkdir()
        sqlite3.connect(data / 'welfare.db').close()
        (data / 'config.json').write_text('{}', encoding='utf-8')
        os.environ.update(LOCALAPPDATA=profile, AI_REQUEST_TIMEOUT_SEC='45')
        try:
            result_code = asyncio.run(run(args))
        finally:
            import database
            import logging
            database.close_all_connections()
            logging.shutdown()
        sys.exit(result_code)

"""Opt-in, synthetic quality smoke: 4 policy + 4 news + 2 embedding requests.

Dry-run is the default and does not read credentials. Execution requires all of
--execute --free-tier-confirmed --config. Free-tier eligibility is a human
confirmation, not something this script can establish. No OCR, Notion, collection,
quota/retry-path, or operational-database behavior is evaluated here.
The ten-attempt cap is per execution. Existing output is never reused; a human
can authorize a separate run with a new output path, which has a separate cap.
"""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

MODEL = 'gemini-3.5-flash-lite'
EMBEDDING_MODEL = 'gemini-embedding-001'
MAX_REQUESTS = 10
MAX_INPUT_CHARS = 16000
MAX_OUTPUT_TOKENS = 3072
TIMEOUT_SECONDS = 45
MIN_INTERVAL_SECONDS = 15
BACKEND = Path(__file__).resolve().parents[1]
WORKSPACE = BACKEND.parent
DEFAULT_OUTPUT = WORKSPACE / 'artifacts' / 'finalist-quality-smoke'
LOCK_PATH = WORKSPACE / 'artifacts' / '.finalist-quality-smoke.lock'


def cases():
    """Versioned, public, synthetic fixtures; no real page or personal data."""
    return [
        dict(id='policy-parking', kind='policy', title='합성 장애인 주차 감면',
             source='등록장애인이 탑승한 차량만 적용. 일반주차장 1시간 면제 후 50% 할인. 환승주차장 3시간 면제 후 80% 할인.',
             expected={'prose_contains': ['등록장애인이 탑승한 차량만 적용', '1시간', '50%', '3시간', '80%'],
                       'equals': {'start_age_months': None, 'end_age_months': None, 'residence': [], 'target_income': ''}}),
        dict(id='policy-infant', kind='policy', title='합성 영아 보육료',
             source='생후 2개월부터 6개월까지 영아에게 월 10만원 지급. 보호자가 방문 신청해야 한다. 다른 보육료와 중복 지원 불가.',
             expected={'prose_contains': ['월 10만원', '보호자가 방문 신청해야 한다', '중복 지원 불가'],
                       'equals': {'start_age_months': 2, 'end_age_months': 6, 'residence': [], 'target_income': ''}}),
        dict(id='policy-senior', kind='policy', title='합성 노인 교통비',
             source='만 65세 이상에게 교통비 월 2만원 지급. 도봉구 거주자만 신청할 수 있다. 본인 명의 계좌가 필요하다.',
             expected={'prose_contains': ['월 2만원', '도봉구 거주자만 신청할 수 있다', '본인 명의 계좌가 필요하다'],
                       'equals': {'start_age_months': 780, 'end_age_months': None, 'residence': ['도봉구'], 'target_income': ''}}),
        dict(id='policy-parent', kind='policy', title='합성 한부모 교육비',
             source='한부모 가구에 교육비 월 5만원 지원. 동일 목적의 다른 교육비와 중복 지원 불가. 보호자가 방문 신청해야 한다.',
             expected={'prose_contains': ['월 5만원', '중복 지원 불가', '보호자가 방문 신청해야 한다'],
                       'equals': {'start_age_months': None, 'end_age_months': None, 'residence': [], 'target_income': ''}}),
        dict(id='news-senior', kind='news', title='합성 노인 교통비 신청',
             source='노년 대상 교통비 월 2만원 지원. 만 65세 이상 도봉구 주민 본인이 신청할 수 있다.',
             expected={'prose_contains': ['월 2만원'], 'equals': {'start_age_months': 780, 'end_age_months': None,
                       'applicant_type': 'individual'}, 'target_status': 'match'}),
        dict(id='news-proxy', kind='news', title='합성 영아 보육료 신청',
             source='생후 2개월부터 6개월까지 영아에게 보육료 월 10만원 지급. 보호자가 영아를 대신하여 신청할 수 있다.',
             expected={'prose_contains': ['월 10만원'], 'equals': {'start_age_months': 2, 'end_age_months': 6,
                       'applicant_type': 'proxy'}, 'target_status': 'match'}),
        dict(id='news-provider', kind='news', title='합성 아동 지원 참여기관 모집',
             source='아동 간식 지원 사업의 참여기관을 모집한다. 신청 대상은 급식 제공업체이다.',
             expected={'equals': {'applicant_type': 'organization', 'start_age_months': None, 'end_age_months': None},
                       'target_status': 'excluded'}),
        dict(id='news-unknown', kind='news', title='합성 지원사업 사전 안내',
             source='교육비 지원 사업을 준비 중이다. 신청 대상과 신청 방법은 추후 공개한다.',
             expected={'equals': {'applicant_type': 'unknown', 'start_age_months': None, 'end_age_months': None,
                       'residence': []}, 'target_status': 'review'}),
        dict(id='embedding-reference', kind='embedding', title='합성 임베딩 기준',
             source='도봉구 주민을 위한 복지 지원 소식을 확인합니다.', expected={'dimensions': 768, 'finite_nonzero': True}),
        dict(id='embedding-identical', kind='embedding', title='합성 동일 문장 임베딩',
             source='도봉구 주민을 위한 복지 지원 소식을 확인합니다.', expected={'dimensions': 768, 'finite_nonzero': True,
                       'identical_text_cosine_min': 0.98}),
    ]


def plan():
    return dict(kind='quality-smoke', fixture_version=1, model=MODEL, embedding_model=EMBEDDING_MODEL,
                max_requests=MAX_REQUESTS, max_input_chars=MAX_INPUT_CHARS,
                max_output_tokens=MAX_OUTPUT_TOKENS, timeout_seconds=TIMEOUT_SECONDS,
                min_interval_seconds=MIN_INTERVAL_SECONDS, sdk_attempts=1, cases=cases(),
                not_evaluated=['OCR/images', 'live pages', 'Notion', 'operational DB',
                               'production quota/retry path', 'embedding relevance calibration'])


class EvaluationStopped(BaseException):
    """Bypass product fallback handlers; never turn a transport error into retry."""


def safe_error(error):
    result = {'type': type(error).__name__}
    for name in ('code', 'status_code'):
        value = getattr(error, name, None)
        if type(value) is int:
            result[name] = value
    return result  # Deliberately never str(error), repr(error), or a traceback.


def generation_record(response):
    usage = getattr(response, 'usage_metadata', None)
    counts = {}
    for key in ('prompt_token_count', 'candidates_token_count', 'total_token_count',
                'cached_content_token_count', 'thoughts_token_count'):
        value = getattr(usage, key, None)
        counts[key] = value if type(value) is int else None
    return dict(text=response.text, usage_metadata=counts,
                model_version=getattr(response, 'model_version', None),
                response_id=getattr(response, 'response_id', None))


class Ledger:
    """Exclusive append-only journal; fsync every event, especially pre-dispatch."""
    def __init__(self, path, secret=''):
        self.path = Path(path)
        self.secret = secret
        self.handle = self.path.open('x', encoding='utf-8', newline='\n')
        self.failed = False

    def append(self, event):
        if self.failed:
            raise EvaluationStopped('LedgerUnavailable')
        try:
            encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
            if self.secret:
                encoded = encoded.replace(self.secret, '[REDACTED]')
            self.handle.write(encoded + '\n')
            self.handle.flush()
            os.fsync(self.handle.fileno())
        except BaseException:
            self.failed = True
            raise EvaluationStopped('LedgerUnavailable') from None

    def close(self):
        self.handle.close()


@contextmanager
def single_run_lock(path=LOCK_PATH):
    """Cross-output-directory OS lock. Keep the inode to avoid unlink races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class RequestBudget:
    def __init__(self, ledger, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.ledger, self.clock, self.sleep = ledger, clock, sleep
        self.attempts, self.last_dispatch, self.stopped = 0, None, False
        self._busy = False

    async def dispatch(self, case_id, kind, contents, invoke, raw):
        if self.stopped or self._busy or self.attempts >= MAX_REQUESTS:
            self.stopped = True
            raise EvaluationStopped('RequestBudgetClosed')
        if not isinstance(contents, str) or not contents.strip() or len(contents) > MAX_INPUT_CHARS:
            self.stopped = True
            raise EvaluationStopped('InputLimit')
        self._busy = True
        try:
            if self.last_dispatch is not None:
                await self.sleep(max(0, MIN_INTERVAL_SECONDS - (self.clock() - self.last_dispatch)))
            self.attempts += 1
            self.ledger.append(dict(event='attempt_reserved', attempt=self.attempts,
                case_id=case_id, kind=kind, input_chars=len(contents),
                prompt_sha256=hashlib.sha256(contents.encode()).hexdigest(),
                monotonic=self.clock(), state='dispatched_or_uncertain'))
            # This line is reachable only after the durable reservation succeeds.
            self.last_dispatch = self.clock()
            response = await asyncio.wait_for(invoke(), timeout=TIMEOUT_SECONDS)
            self.ledger.append(dict(event='response', attempt=self.attempts, case_id=case_id, raw=raw(response)))
            return response
        except EvaluationStopped:
            self.stopped = True
            raise
        except BaseException as error:
            self.stopped = True
            self.ledger.append(dict(event='request_failed', attempt=self.attempts,
                                    case_id=case_id, error=safe_error(error)))
            raise EvaluationStopped('RequestFailed') from None
        finally:
            self._busy = False


def check_expected(case, result, decision=None):
    expected = case['expected']
    checks = {'production_validation': isinstance(result, dict)}
    if not isinstance(result, dict):
        return checks
    for key, value in expected.get('equals', {}).items():
        checks['equals.' + key] = key in result and result[key] == value
    prose = ''.join(str(result.get(k) or '') for k in ('support_detail', 'eligibility_detail', 'age_detail', 'target_income'))
    compact = lambda value: re.sub(r'\s+', '', value)
    for value in expected.get('prose_contains', []):
        checks['contains.' + value] = compact(value) in compact(prose)
    if 'target_status' in expected:
        checks['target_status'] = decision is not None and decision['target_status'] == expected['target_status']
    return checks


def runtime_versions():
    from services import news_custom_analysis, news_analysis_safety
    package = json.loads((WORKSPACE / 'welfare-tauri' / 'package.json').read_text(encoding='utf-8'))
    paths = ['ai_classifier.py', 'services/policy_evidence.py', 'services/news_custom_analysis.py',
             'services/news_analysis_safety.py', 'services/news_semantic_scorer.py']
    return dict(app_version=package['version'], news_recipe=news_custom_analysis.RECIPE,
                news_validation=news_analysis_safety.VERSION, python=sys.version.split()[0],
                google_genai=version('google-genai'),
                source_sha256={p: hashlib.sha256((BACKEND / p).read_bytes()).hexdigest() for p in paths})


async def evaluate(client, ledger, *, budget=None):
    """Production prompts and post-validation; only transport/admission replaced."""
    import ai_classifier
    from google.genai import types
    from services import ai_coordinator, news_custom_analysis, news_analysis_safety
    from services.news_analysis_worker import validate_result
    from services.news_profile import NewsProfile
    from services.news_semantic_scorer import EmbeddingRecipe, SemanticScorer
    budget = budget or RequestBudget(ledger)
    current, completed, reference = None, 0, None

    async def once(_client, prompt, title, *, model_override=None, response_json_schema=None):
        if model_override not in (None, MODEL):
            raise EvaluationStopped('ModelOverrideForbidden')
        config = types.GenerateContentConfig(response_mime_type='application/json',
            response_json_schema=response_json_schema, temperature=0, max_output_tokens=MAX_OUTPUT_TOKENS)
        response = await budget.dispatch(current['id'], 'generate', prompt,
            lambda: client.aio.models.generate_content(model=MODEL, contents=prompt, config=config),
            generation_record)
        text = response.text or ''
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.IGNORECASE)
        return json.loads(text)

    async def embed_once(*, model, contents, config):
        if model != EMBEDDING_MODEL:
            raise EvaluationStopped('ModelOverrideForbidden')
        return await budget.dispatch(current['id'], 'embedding', contents,
            lambda: client.aio.models.embed_content(model=model, contents=contents, config=config),
            lambda result: [embedding.values for embedding in (result.embeddings or [])])

    async def no_admission(*_args, **_kwargs):
        return None

    async def direct(awaitable, **_kwargs):
        return await awaitable

    proxy = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed_once)))
    scorer = SemanticScorer(proxy, EmbeddingRecipe(EMBEDDING_MODEL))
    ledger.append(dict(event='runtime', versions=runtime_versions(), embedding_recipe=scorer.scoring_version))
    try:
        with patch.object(ai_classifier, '_call_gemini_json', once), \
                patch.object(ai_coordinator, 'acquire_model_slot', no_admission), \
                patch.object(ai_coordinator, 'await_api', direct), \
                patch.object(ai_coordinator, 'register_api_error', lambda *_a, **_k: None):
            for current in cases():
                decision = None
                if current['kind'] == 'policy':
                    result = await ai_classifier.extract_policy_fields_async(client, current['title'],
                        'https://example.invalid/synthetic', site_name='합성 시험', content=current['source'])
                    checks = check_expected(current, result)
                elif current['kind'] == 'news':
                    profile = NewsProfile('synthetic-smoke', 1, '합성 시험', '전국')
                    options = news_custom_analysis.recipe_options(news_custom_analysis.RECIPE)
                    flags = {k: options[k] for k in ('legacy_input', 'evidence_rules', 'applicant_rules',
                                                     'structured_output', 'grounding_rules', 'complete_grounding_rules')}
                    payload = dict(title=current['title'], content=current['source'],
                                   link='https://example.invalid/synthetic', site_name='합성 시험')
                    result = await news_custom_analysis.classify(client, profile, payload, MODEL, **flags)
                    validate_result(result)
                    news_custom_analysis.validate_facts(result, require_interest_topics=options['require_interest_topics'])
                    news_custom_analysis.applicant_status(result, current['source'])
                    decision = news_analysis_safety.assess(profile, result, current['source'])
                    result = decision['analysis']
                    checks = check_expected(current, result, decision)
                else:
                    vector = await scorer._embed(current['source'])
                    result = dict(dimensions=len(vector), normalized_norm=math.hypot(*vector))
                    checks = dict(production_validation=True, dimensions=len(vector) == 768,
                                  finite_nonzero=all(math.isfinite(v) for v in vector) and math.hypot(*vector) > 0)
                    if reference is None:
                        reference = vector
                    else:
                        result['identical_text_cosine'] = math.fsum(a * b for a, b in zip(reference, vector))
                        checks['identical_text_cosine_min'] = result['identical_text_cosine'] >= 0.98
                ledger.append(dict(event='case_result', case_id=current['id'], result=result,
                                   decision=decision, checks=checks, passed=all(checks.values())))
                if not all(checks.values()):
                    raise EvaluationStopped('QualityCheckFailed')
                completed += 1
    except BaseException as error:
        budget.stopped = True
        ledger.append(dict(event='stopped', error=safe_error(error), completed=completed,
                           attempts=budget.attempts, remaining=[case['id'] for case in cases()[completed:]]))
        return dict(status='stopped', attempts=budget.attempts, completed=completed)
    ledger.append(dict(event='complete', attempts=budget.attempts, completed=completed))
    return dict(status='complete', attempts=budget.attempts, completed=completed)


class IsolationGuard:
    """Fail closed on operational .env, external writes/DBs and other services."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.active = True
        self.google_addresses = set()
        self.loopback_sockets = []

    def local_port(self, port):
        for sock in self.loopback_sockets:
            try:
                if sock.getsockname()[1] == port:
                    return True
            except OSError:
                pass
        return False

    def owned(self, value):
        if isinstance(value, int):
            return True
        try:
            return Path(os.fsdecode(value)).resolve().is_relative_to(self.root)
        except (TypeError, ValueError):
            return False

    def __call__(self, event, args):
        if not self.active:
            return
        if event == 'open':
            path, mode, flags = args
            if not isinstance(path, int) and Path(os.fsdecode(path)).name.casefold().startswith('.env'):
                raise PermissionError('EnvironmentFileBlocked')
            if ((mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)) and not self.owned(path):
                raise PermissionError('ExternalWriteBlocked')
        elif event == 'sqlite3.connect' and args[0] != ':memory:' and not self.owned(args[0]):
            raise PermissionError('ExternalDatabaseBlocked')
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate', 'os.utime'}:
            if not self.owned(args[0]):
                raise PermissionError('ExternalMutationBlocked')
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            if not all(self.owned(p) for p in args[:2]):
                raise PermissionError('ExternalMutationBlocked')
        elif event in {'subprocess.Popen', 'os.system', 'os.startfile', 'os.exec', 'os.spawn'}:
            raise PermissionError('SubprocessBlocked')
        elif event == 'socket.getaddrinfo':
            if args[0] in ('127.0.0.1', '::1', 'localhost') and self.local_port(args[1]):
                return  # Windows asyncio self-pipe, only a socket bound here.
            if args[0] not in ('generativelanguage.googleapis.com', b'generativelanguage.googleapis.com'):
                raise PermissionError('ExternalHostBlocked')
        elif event == 'socket.bind':
            if args[1][0] not in ('127.0.0.1', '::1') or args[1][1] != 0:
                raise PermissionError('ListenerBlocked')
            self.loopback_sockets.append(args[0])
        elif event == 'socket.connect':
            address = args[1]
            if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1') and self.local_port(address[1]):
                return
            if not (isinstance(address, tuple) and len(address) >= 2 and address[1] == 443
                    and address[0] in self.google_addresses):
                raise PermissionError('ExternalConnectionBlocked')


@contextmanager
def isolated_runtime(output):
    # A standalone process is required: never close or reconfigure live app state.
    if any(name in sys.modules for name in ('database', 'ai_classifier', 'utils')):
        raise RuntimeError('StandaloneProcessRequired')
    output = Path(output).resolve()
    profile, work, temp = output / 'profile', output / 'work', output / 'temp'
    for path in (profile / 'welfare-scraper', work, temp):
        path.mkdir(parents=True)
    sqlite3.connect(profile / 'welfare-scraper' / 'welfare.db').close()
    (profile / 'welfare-scraper' / 'config.json').write_text('{}', encoding='utf-8')
    saved_env, saved_cwd, saved_temp = dict(os.environ), Path.cwd(), tempfile.tempdir
    saved_path, saved_bytecode = list(sys.path), sys.dont_write_bytecode
    saved_logging = logging.root.manager.disable
    guard = IsolationGuard(output)
    original_resolve = socket.getaddrinfo

    def resolve(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        if args[0] in ('generativelanguage.googleapis.com', b'generativelanguage.googleapis.com'):
            guard.google_addresses.update(item[4][0] for item in result)
        return result

    try:
        for key in tuple(os.environ):
            if any(part in key.upper() for part in ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD', 'WEBHOOK', 'PROXY')) or key.startswith('WELFARE_DESKTOP_'):
                os.environ.pop(key, None)
        os.environ.update(LOCALAPPDATA=str(profile), APPDATA=str(profile), TEMP=str(temp), TMP=str(temp),
                          PYTHON_DOTENV_DISABLED='1', GOOGLE_API_KEY='', NOTION_TOKEN='', NOTION_POLICY_DBS='[]',
                          CLASSIFY_MODEL=MODEL, AI_CONTENT_MAX_CHARS='6000')
        tempfile.tempdir = str(temp)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(BACKEND))
        logging.disable(logging.CRITICAL)
        os.chdir(work)
        sys.addaudithook(guard)
        import dotenv
        with patch.object(dotenv, 'load_dotenv', lambda *_a, **_k: False), \
                patch.object(dotenv, 'dotenv_values', lambda *_a, **_k: {}), \
                patch.object(socket, 'getaddrinfo', resolve):
            yield
    finally:
        if 'database' in sys.modules:
            sys.modules['database'].close_all_connections()
        logging.shutdown()
        guard.active = False
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)
        tempfile.tempdir = saved_temp
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_bytecode
        logging.disable(saved_logging)


async def execute_with_client(key, ledger):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
        base_url='https://generativelanguage.googleapis.com',
        timeout=TIMEOUT_SECONDS * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
    try:
        return await evaluate(client, ledger)
    finally:
        try:
            await client.aio.aclose()
        finally:
            client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--free-tier-confirmed', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(dict(status='dry-run', requests=0, plan=plan()), ensure_ascii=True))
        return 0
    if not args.free_tier_confirmed or args.config is None:
        print(json.dumps(dict(status='refused', requests=0, reason='ExplicitExecutionGatesRequired')))
        return 2
    ledger = None
    try:
        with single_run_lock():
            # Read only the explicitly selected JSON key. Never read app .env or
            # import user settings into the disposable profile.
            if args.config.name.casefold().startswith('.env'):
                raise ValueError('JSONConfigRequired')
            with args.config.open('r', encoding='utf-8-sig') as handle:
                key = json.load(handle).get('GOOGLE_API_KEY')
            if not isinstance(key, str) or not key.strip():
                raise ValueError('MissingGoogleKey')
            output = args.output_dir.resolve()
            output.mkdir(parents=True, exist_ok=False)
            ledger = Ledger(output / 'ledger.jsonl', secret=key)
            ledger.append(dict(event='plan', **plan()))
            with isolated_runtime(output):
                result = asyncio.run(execute_with_client(key, ledger))
            print(json.dumps(result, ensure_ascii=True))
            return 0 if result['status'] == 'complete' else 1
    except BaseException as error:
        print(json.dumps(dict(status='refused-or-stopped', error=safe_error(error)), ensure_ascii=True))
        return 2
    finally:
        if ledger is not None:
            ledger.close()


if __name__ == '__main__':
    raise SystemExit(main())

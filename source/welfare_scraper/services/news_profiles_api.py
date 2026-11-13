"""News profile management, run preparation, and collection workflow API."""
import asyncio
import json
import os
import uuid
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from services.news_jobs_api import _execute
from services import news_profile_store as store
from services.news_profile import NewsProfile
from services.news_profile import capture_legacy_profile

router = APIRouter(prefix='/api/news', tags=['news-profiles'])


@router.get('/profile-taxonomy')
async def get_profile_taxonomy():
    from services.news_profile_taxonomy import public_taxonomy
    return public_taxonomy()


@router.post('/profiles/legacy/capture')
async def capture_existing_settings():
    def capture(conn):
        import utils
        import content_filter
        from services import relevance_filter
        with utils._CONFIG_LOCK, store._atomic(conn):
            if conn.execute("SELECT 1 FROM news_profiles WHERE profile_id='legacy-infant'").fetchone():
                return _summary(store.get_profile(conn, 'legacy-infant'))
            # Preserve effective cached keywords if present, without triggering
            # default keyword writes as a side effect of taking this copy.
            keywords = content_filter._keyword_cache
            if keywords is None:
                keywords = [r[0] for r in conn.execute('SELECT keyword FROM keywords')]
                keywords = keywords or content_filter.DEFAULT_KEYWORDS
            excluded = content_filter._excluded_keyword_cache
            if excluded is None:
                excluded = [r[0] for r in conn.execute('SELECT keyword FROM excluded_keywords')]
                excluded = excluded or content_filter.DEFAULT_EXCLUDED_KEYWORDS
            cfg = relevance_filter.load_config()
            profile = capture_legacy_profile(
                region=cfg['target_region'], max_age_months=cfg['max_months'],
                keywords=keywords, excluded_keywords=excluded,
                site_ids=[str(r[0]) for r in conn.execute('SELECT id FROM sites ORDER BY id')],
                ai_prompt=utils.get_env_value('CUSTOM_AI_PROMPT'),
                semantic_text=relevance_filter.build_profile_text(cfg),
                relevance_enabled=cfg['enabled'])
            return _summary(store.initialize_legacy(conn, profile))
    return await _execute(capture)


class ProfileSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=100)
    region: StrictStr = Field(min_length=1, max_length=100)
    min_age_months: StrictInt | None = Field(default=None, ge=0, le=1800)
    max_age_months: StrictInt | None = Field(default=None, ge=0, le=1800)
    life_cycles: list[StrictStr] = Field(default_factory=list, max_length=30)
    characteristics: list[StrictStr] = Field(default_factory=list, max_length=50)
    interest_topics: list[StrictStr] = Field(default_factory=list, max_length=30)
    keywords: list[StrictStr] = Field(default_factory=list, max_length=500)
    excluded_keywords: list[StrictStr] = Field(default_factory=list, max_length=500)
    site_ids: list[StrictStr] = Field(default_factory=list, max_length=500)
    sources: list[StrictStr] = Field(default_factory=lambda: ['sites'], max_length=2)
    ai_prompt: StrictStr = Field(default='', max_length=10000)
    semantic_text: StrictStr = Field(default='', max_length=5000)
    relevance_enabled: StrictBool = True
    publication_period: StrictStr = Field(default='30', pattern=r'^(7|30|90|custom)$')
    publication_start_date: StrictStr = Field(default='', max_length=10)
    publication_end_date: StrictStr = Field(default='', max_length=10)


class EditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=1)
    settings: ProfileSettings


class DefaultConditionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0)
    settings: ProfileSettings


@router.get('/default-condition')
async def get_default_condition():
    from services import news_default_condition as defaults
    def read(conn):
        profile = defaults.read(conn)
        if profile:
            return {'profile': profile.snapshot(), 'legacy_collection_available': False}
        import utils
        available = defaults.legacy_collection_available(conn)
        values = utils.load_config_file()
        keywords, excluded = (), ()
        if available:
            import content_filter
            # Existing users see their effective old values before explicitly
            # saving a current condition. Fresh users do not inherit defaults.
            values = {key: utils.get_env_value(key) for key in (
                'TARGET_REGION', 'TARGET_AGE_MAX_MONTHS', 'CUSTOM_AI_PROMPT',
                'RELEVANCE_PROFILE', 'RELEVANCE_FILTER_ENABLED')}
            values = {key:value for key,value in values.items() if value != ''}
            keywords, excluded = content_filter.DEFAULT_KEYWORDS, content_filter.DEFAULT_EXCLUDED_KEYWORDS
        return {'profile': None, 'legacy_collection_available': available,
                'initial_settings': defaults.initial_settings(conn, values, legacy=available,
                    default_keywords=keywords, default_excluded=excluded)}
    return await _execute(read)


@router.put('/default-condition')
async def save_default_condition(request: DefaultConditionRequest):
    from services import news_default_condition as defaults
    def save(conn):
        with store._atomic(conn):
            values = request.settings.model_dump()
            values['site_ids'] = [str(row[0]) for row in conn.execute('SELECT id FROM sites ORDER BY id')]
            profile = _custom_profile(defaults.PROFILE_ID, request.expected_revision + 1, ProfileSettings(**values))
            return defaults.save(conn, profile, request.expected_revision).snapshot()
    return {'profile': await _execute(save)}


async def start_default_condition(*, headless=True, wait=False):
    """Shared manual/scheduled entry. None means proven, unmigrated legacy use.

    A configured but unready condition must fail closed, never run legacy rules.
    """
    return await _with_workflow_ownership(
        lambda ownership: _start_default_condition(headless=headless, wait=wait, ownership=ownership))


async def _start_default_condition(*, headless, wait, ownership):
    from services import news_default_condition as defaults
    profile = await _owned_execute(defaults.synchronize_sites)
    if profile is None:
        if not await _owned_execute(defaults.legacy_collection_available):
            raise HTTPException(409, '새 소식 수집 조건을 먼저 저장해주세요. 설정의 수집 조건에서 지역과 대상을 확인할 수 있습니다.')
        return None
    options = await _settled_result(workflow_options(profile.profile_id))
    if not options['enabled']:
        raise HTTPException(409, options['reason'])
    def waiting_run(conn):
        from services.news_profile_workflow import ensure_schema, get_workflow
        ensure_schema(conn)
        rows=conn.execute('''SELECT w.workflow_id FROM news_profile_workflows w
            JOIN news_run_snapshots r ON r.run_id=w.run_id
            WHERE r.profile_id=? AND r.revision=? AND w.state IN ('waiting','failed')
            ORDER BY w.rowid DESC''',(profile.profile_id,profile.revision)).fetchall()
        return next((row[0] for row in rows if get_workflow(conn, row[0])['can_resume']), None)
    waiting_id = await _owned_execute(waiting_run) if wait else None
    result = (await _resume_workflow(waiting_id, ownership=ownership, allow_initial_setup=False) if waiting_id else
        await _start_profile_workflow(profile.profile_id,
            StartWorkflowRequest(expected_revision=profile.revision, headless=headless), ownership=ownership))
    if wait:
        task = _WORKFLOW_TASKS.get(result['workflow_id'])
        if task is not None:
            await task
        # Admission is not completion. The command-line caller needs the durable
        # outcome even if a fast task's done callback has already removed it.
        from services.news_profile_workflow import get_workflow
        final = await _owned_execute(lambda conn: get_workflow(conn, result['workflow_id']))
        result = {**result, 'state': final['state'], 'workflow': final}
    return result


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=100)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=1)


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=1)
    headless: StrictBool = True
    new_collection: StrictBool = False


def _custom_profile(profile_id, revision, settings):
    """Target conditions never authorize sources beyond registered sites."""
    values = settings.model_dump()
    from services.news_auto_sources import automatic_sources
    values['sources'] = ['sites']
    values['bokjiro_mode'] = 'unsupported'
    return automatic_sources(NewsProfile(profile_id=profile_id, revision=revision, **values))


def _configured_semantic_threshold():
    value = os.getenv('NEWS_PROFILE_SEMANTIC_THRESHOLD')
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if -1 <= result <= 1 else None


def _runtime_workflow_settings():
    model = os.getenv('CLASSIFY_MODEL') or 'gemini-3.5-flash-lite'
    if not model.strip() or model != model.strip():
        return None
    threshold = _configured_semantic_threshold()
    if threshold is None:
        # Invalid explicit configuration must not silently switch modes.
        if os.getenv('NEWS_PROFILE_SEMANTIC_THRESHOLD') is not None:
            return None
        return {'mode': 'direct-body-bounded-v1',
                'analysis_model': model,
                'headless': True}
    try:
        dimensions = int(os.getenv('EMBED_DIM', '768'))
    except (TypeError, ValueError):
        return None
    if not 1 <= dimensions <= 3072:
        return None
    embedding_model = os.getenv('EMBED_MODEL', 'gemini-embedding-001')
    if not embedding_model.strip() or embedding_model != embedding_model.strip():
        return None
    return {'analysis_model': model,
            'embedding_model': embedding_model,
            'embedding_dimensions': dimensions,
            'semantic_threshold': threshold, 'headless': True}


def _offline_workflow_settings(*, headless=True):
    if type(headless) is not bool:
        raise ValueError('headless must be boolean')
    return {'mode': 'bokjiro-official-no-ai-v1', 'analysis_model': 'not-used',
            'embedding_model': 'not-used', 'embedding_dimensions': 0,
            'semantic_threshold': 0.0, 'headless': headless}


_ANALYSIS_WAIT_MESSAGES = {
    'authentication_required':'원문은 먼저 수집할 수 있습니다. AI 연결을 확인한 뒤 저장한 자료의 분석을 이어서 진행해주세요.',
    'analysis_settings_required':'원문은 먼저 수집할 수 있습니다. AI 분석 설정을 준비한 뒤 저장한 자료의 분석을 이어서 진행해주세요.',
    'analysis_settings_changed':'이 수집의 원문은 보존됩니다. 처음 확정한 AI 분석 설정을 복원한 뒤 분석을 이어서 진행해주세요.',
    'local_budget_exhausted':'저장된 원문은 보존됩니다. 이어서 진행해 남은 자료를 분석할 수 있습니다.',
    'daily_quota_exhausted':'원문은 먼저 수집할 수 있습니다. AI 제공업체 한도가 다시 사용 가능해진 뒤 분석을 이어서 진행해주세요.',
    'ai_usage_state_unavailable':'원문은 먼저 수집할 수 있습니다. AI 사용 기록을 확인할 수 없어 분석은 보류합니다. 연결 관리에서 확인해주세요.',
}


def _analysis_client():
    # Constructing a client is not an API call. A missing/broken key must not
    # prevent the independent local collection stage from starting.
    import ai_classifier
    try:
        return ai_classifier.get_client()
    except Exception:
        return None


def _analysis_preflight(settings, *, client, profile=None):
    if settings.get('mode') == 'analysis-setup-required-v1':
        return 'analysis_settings_required'
    if settings.get('mode') == 'bokjiro-official-no-ai-v1':
        return None
    runtime = _runtime_workflow_settings()
    if runtime is None:
        return 'analysis_settings_required'
    runtime['headless'] = settings.get('headless', True)
    if settings != runtime:
        return 'analysis_settings_changed'
    if profile is not None and settings.get('mode') != 'direct-body-bounded-v1':
        from services.news_analysis_context import NewsAnalysisContext
        if not NewsAnalysisContext(profile).semantic_text.strip():
            return 'analysis_settings_required'
    if client is None:
        return 'authentication_required'
    from services import ai_coordinator
    try:
        usage = ai_coordinator.status()
    except Exception:
        return 'ai_usage_state_unavailable'
    if not isinstance(usage, dict) or usage.get('state_warning'):
        return 'ai_usage_state_unavailable'
    models = [settings['analysis_model']]
    if settings.get('embedding_model'):
        models.append(settings['embedding_model'])
    model_usage = usage.get('models', {})
    if not isinstance(model_usage, dict):
        return 'ai_usage_state_unavailable'
    selected_usage = [model_usage.get(model, {}) for model in [*models, '*']]
    if any(not isinstance(value, dict) for value in selected_usage):
        return 'ai_usage_state_unavailable'
    if any(value.get('daily_exhausted') for value in selected_usage):
        return 'daily_quota_exhausted'
    return None


def _pending_workflow_settings(*, headless=True):
    return {'mode':'analysis-setup-required-v1', 'analysis_model':'analysis-not-configured',
            'headless':headless}


def _workflow_dependencies(client, settings, *, analysis_wait_reason=None):
    extra = {'analysis_wait_reason':analysis_wait_reason} if analysis_wait_reason else {}
    if settings.get('mode') == 'analysis-setup-required-v1':
        return dict(client=None, scorer=None, analysis_model=settings['analysis_model'],
                    threshold=None, headless=settings['headless'], defer_analysis=True,
                    analysis_wait_reason='analysis_settings_required')
    if settings.get('mode') == 'direct-body-bounded-v1':
        from services.news_direct_analysis import DirectAnalysis
        return dict(client=client, scorer=DirectAnalysis(), analysis_model=settings['analysis_model'],
                    threshold=None, headless=settings['headless'], **extra)
    if settings.get('mode') == 'bokjiro-official-no-ai-v1':
        return dict(client=None, scorer=None, analysis_model='not-used', threshold=0.0,
                    headless=settings['headless'])
    from services.news_semantic_scorer import SemanticScorer, EmbeddingRecipe
    recipe = EmbeddingRecipe(settings['embedding_model'], dimensions=settings['embedding_dimensions'])
    return dict(client=client, scorer=SemanticScorer(client, recipe) if client is not None else None,
                analysis_model=settings['analysis_model'], threshold=settings['semantic_threshold'],
                headless=settings['headless'], **extra)


_WORKFLOW_TASKS = {}
_WORKFLOW_OWNERS = {}


async def _settled_result(awaitable):
    from services.news_analysis_worker import _settle_db
    result, cancelled = await _settle_db(awaitable)
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _owned_execute(action):
    # Even a schema-ensuring read can write. No DB worker may outlive the
    # admission coroutine and its not-yet-transferred OS lease.
    return await _settled_result(_execute(action))


async def _acquire_workflow_ownership():
    """Only an exclusive whole-workflow owner may reclaim interrupted rows."""
    from services import app_lifecycle, news_workflow_ownership
    from services.news_profile_workflow import recover_interrupted_workflows
    from services.news_analysis_worker import _settle_db
    if app_lifecycle.shutdown_requested:
        raise HTTPException(503, '프로그램을 종료하는 중입니다.')
    ownership = news_workflow_ownership.acquire()
    if ownership is None:
        raise HTTPException(409, detail={'error': 'WORKFLOW_OWNER_UNAVAILABLE',
            'message': '다른 수집 작업이 진행 중이거나 작업 잠금을 확인할 수 없습니다. 잠시 후 다시 시도해주세요.'})
    try:
        _, cancelled = await _settle_db(_execute(recover_interrupted_workflows))
        if cancelled:
            raise asyncio.CancelledError
        return ownership
    except BaseException:
        ownership.close()
        raise


async def _with_workflow_ownership(action):
    ownership = await _acquire_workflow_ownership()
    try:
        # A wait=True caller still reads the durable result after the task's
        # done callback requests close. Keep that final DB read/drain in the
        # same lease; non-wait admission releases only its pin, not a live task.
        with ownership.pin():
            return await action(ownership)
    finally:
        if not ownership.transferred:
            ownership.close()
        elif not any(owner is ownership for owner in _WORKFLOW_OWNERS.values()):
            # wait=True may keep the final read pinned after the done callback.
            # Retry scheduling only after that completed workflow's pin exits.
            from services.news_record_retention import schedule_configured_cleanup
            schedule_configured_cleanup()


async def recover_idle_workflows():
    """Startup recovery is best-effort only when no live workflow owns the DB."""
    ownership = await _acquire_workflow_ownership()
    ownership.close()


def _consume_workflow_task(workflow_id, task, ownership=None):
    if _WORKFLOW_TASKS.get(workflow_id) is task:
        _WORKFLOW_TASKS.pop(workflow_id, None)
        _WORKFLOW_OWNERS.pop(workflow_id, None)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        # State/error code is persisted by run_workflow. Never log raw provider input here.
        pass
    finally:
        if ownership is not None:
            if _WORKFLOW_OWNERS.get(workflow_id) is ownership:
                _WORKFLOW_OWNERS.pop(workflow_id, None)
            ownership.close()
        # After release: collection ownership is never bypassed for cleanup.
        from services.news_record_retention import schedule_configured_cleanup
        schedule_configured_cleanup()


def _launch_workflow(workflow_id, *, ownership=None, **kwargs):
    # Recheck at the synchronous launch boundary: concurrent resumes both await DB reads.
    existing = _WORKFLOW_TASKS.get(workflow_id)
    if existing is not None and not existing.done():
        raise HTTPException(409, '이 작업은 이미 진행 중입니다.')
    # Normal callers transfer an admission lease. Keep direct internal callers
    # protected as well; no recovery occurs here after a run has been created.
    if ownership is None:
        from services import news_workflow_ownership
        ownership = news_workflow_ownership.acquire()
        if ownership is None:
            raise HTTPException(409, '다른 수집 작업이 진행 중이거나 작업 잠금을 확인할 수 없습니다.')
    coroutine = _run_workflow(workflow_id, **kwargs)
    try:
        task = asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        ownership.close()
        raise
    ownership.transferred = True
    _WORKFLOW_TASKS[workflow_id] = task
    _WORKFLOW_OWNERS[workflow_id] = ownership
    # A callback also runs if cancellation happens before the coroutine starts.
    # The worker drains DB cancellation before it becomes done.
    task.add_done_callback(lambda done: _consume_workflow_task(workflow_id, done, ownership))


async def _run_workflow(workflow_id, **kwargs):
    from services.news_profile_workflow import run_workflow
    return await run_workflow(workflow_id, **kwargs)


class TargetPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    settings: ProfileSettings
    facts: dict


@router.post('/bokjiro-sample')
async def preview_bokjiro_sample():
    """Retired news-only route; never query the provider or schedule work."""
    from services.news_auto_sources import RETIRED_NEWS_BOKJIRO_MESSAGE
    raise HTTPException(410, RETIRED_NEWS_BOKJIRO_MESSAGE)


class SampleCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: StrictStr = Field(min_length=1, max_length=500)
    facts: dict


class SampleTrialRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=1)
    candidates: list[SampleCandidate] = Field(min_length=1, max_length=20)


@router.post('/profiles/{profile_id}/sample-trial')
async def sample_trial(profile_id: str, request: SampleTrialRequest):
    """Offline candidate-routing trial, not a crawl or AI extraction quality test."""
    from services.news_analysis_context import NewsAnalysisContext
    from services.news_target_rules import evaluate_target
    def trial(conn):
        profile = store.get_profile(conn, profile_id)
        if profile.revision != request.expected_revision:
            raise ValueError('Profile changed')
        context = NewsAnalysisContext(profile)
        results = []
        for candidate in request.candidates:
            route, matched = context.route_title(candidate.title)
            if route == 'excluded':
                status, checks = 'excluded', {'keyword': 'excluded'}
            elif route == 'semantic_candidate':
                # No embeddings are called in an offline trial. Never pretend
                # a keyword miss proves semantic irrelevance or relevance.
                status, checks = 'review', {'semantic_gate': 'not_run'}
            elif profile.general_mode == 'custom_v1':
                decision = evaluate_target(profile, candidate.facts)
                status, checks = decision.status, dict(decision.checks)
            else:
                _, passed, reason = context.post_filter(candidate.facts)
                status, checks = ('match' if passed else 'excluded'), {'legacy_reason': reason}
            results.append({'title': candidate.title, 'route': route,
                            'matched_keywords': matched, 'status': status, 'checks': checks})
        return {'profile_id': profile_id, 'revision': profile.revision, 'results': results,
                'saved': False, 'ai_called': False, 'network_collection': False}
    return await _execute(trial)


@router.post('/target-preview')
async def preview_target(request: TargetPreviewRequest):
    from services.news_target_rules import evaluate_target
    from fastapi import HTTPException
    try:
        profile = NewsProfile(profile_id='preview', revision=1, **request.settings.model_dump())
        decision = evaluate_target(profile, request.facts)
        return {'status': decision.status, 'checks': dict(decision.checks),
                'saved': False, 'ai_called': False}
    except ValueError:
        raise HTTPException(422, '대상 조건과 예시 정보를 확인해주세요.')


def _summary(profile, archived=False):
    execution_enabled = (
        not archived
        and profile.profile_id != 'legacy-infant'
        and profile.general_mode == 'custom_v1'
    )
    return {'profile_id': profile.profile_id, 'revision': profile.revision,
            'name': profile.name, 'archived': archived,
            'region': profile.region, 'min_age_months': profile.min_age_months,
            'max_age_months': profile.max_age_months,
            'life_cycles': list(profile.life_cycles),
            'characteristics': list(profile.characteristics),
            'protected': profile.profile_id in ('legacy-infant', 'default-news'),
            'execution_enabled': execution_enabled}


@router.get('/profiles')
async def list_profiles(include_archived: bool = Query(False)):
    return await _execute(lambda conn: {'items': [_summary(p, archived) for p, archived
        in store.list_profiles(conn, include_archived=include_archived)]})


@router.get('/profiles/{profile_id}')
async def get_profile(profile_id: str):
    # Full settings are intentional for authenticated editing, not telemetry.
    def read(conn):
        profile = store.get_profile(conn, profile_id)
        archived = bool(conn.execute(
            'SELECT archived FROM news_profiles WHERE profile_id=?',
            (profile_id,),
        ).fetchone()[0])
        return {'profile': profile.snapshot(),
                'execution_enabled': _summary(profile, archived)['execution_enabled']}
    return await _execute(read)


@router.post('/profiles')
async def create_profile(request: ProfileSettings):
    def create(conn):
        profile = _custom_profile(uuid.uuid4().hex, 1, request)
        return _summary(store.create_profile(conn, profile))
    return await _execute(create)


@router.put('/profiles/{profile_id}')
async def edit_profile(profile_id: str, request: EditRequest):
    if profile_id == 'default-news':
        raise HTTPException(409, '기본 수집 조건은 수집 설정에서 변경해주세요.')
    def edit(conn):
        # User edits are custom rules, not a claim of verified legacy equivalence.
        profile = _custom_profile(profile_id, request.expected_revision + 1, request.settings)
        return _summary(store.save_revision(conn, profile, expected_revision=request.expected_revision))
    return await _execute(edit)


@router.post('/profiles/{profile_id}/clone')
async def clone_profile(profile_id: str, request: CloneRequest):
    return await _execute(lambda conn: _summary(store.clone_profile(
        conn, profile_id, new_id=uuid.uuid4().hex, name=request.name)))


@router.post('/profiles/{profile_id}/archive')
async def archive_profile(profile_id: str):
    from services.news_default_condition import PROFILE_ID
    if profile_id == PROFILE_ID:
        raise HTTPException(409, '현재 기본 수집 조건은 보관할 수 없습니다. 수집 설정에서 변경해주세요.')
    def archive(conn):
        store.archive_profile(conn, profile_id)
        return _summary(store.get_profile(conn, profile_id), True)
    return await _execute(archive)


@router.post('/profiles/{profile_id}/prepare')
async def prepare_run(profile_id: str, request: PrepareRequest):
    def prepare(conn):
        with store._atomic(conn):
            profile, sites, reason = _collection_selection(conn, profile_id)
            if reason:
                raise HTTPException(409, reason)
            if profile.revision != request.expected_revision:
                raise ValueError('Profile changed')
            run_id = store.prepare_run(conn, profile_id, expected_revision=request.expected_revision, sites=sites)
        return {'run_id': run_id, 'state': 'prepared', 'scheduled': False,
                'execution_enabled': True}
    return await _execute(prepare)


@router.post('/profiles/{profile_id}/workflows')
async def start_profile_workflow(profile_id: str, request: StartWorkflowRequest):
    return await _with_workflow_ownership(
        lambda ownership: _start_profile_workflow(profile_id, request, ownership=ownership))


async def _start_profile_workflow(profile_id: str, request: StartWorkflowRequest, *, ownership):
    """Start an isolated collection workflow; existing /api/scrape remains unchanged."""
    from services import app_lifecycle
    from services.news_profile_workflow import create_workflow
    if app_lifecycle.shutdown_requested:
        raise HTTPException(409, '프로그램을 종료하는 중입니다.')
    profile_for_mode = await _owned_execute(lambda conn: store.get_profile(conn, profile_id))
    ai_required = 'sites' in profile_for_mode.sources
    client = _analysis_client() if ai_required else None
    if ai_required:
        settings = _runtime_workflow_settings() or _pending_workflow_settings(headless=request.headless)
        settings['headless'] = request.headless
        from services.news_analysis_context import NewsAnalysisContext
        if (settings.get('embedding_model')
                and not NewsAnalysisContext(profile_for_mode).semantic_text.strip()):
            settings = _pending_workflow_settings(headless=request.headless)
    else:
        settings = _offline_workflow_settings(headless=request.headless)
    def create(conn):
        with store._atomic(conn):
            profile, sites, reason = _collection_selection(conn, profile_id)
            if reason:
                raise HTTPException(409, reason)
            if (profile.revision != request.expected_revision
                    or profile.fingerprint != profile_for_mode.fingerprint):
                raise ValueError('Profile changed')
            from services.news_profile_workflow import ensure_schema
            ensure_schema(conn)
            waiting, can_collect_new = _collection_backlog(conn, profile_id, profile.revision)
            if waiting and not (request.new_collection and can_collect_new):
                raise HTTPException(409, '이 조건의 진행 중이거나 대기 중인 작업이 있습니다. 이어서 진행하거나 종료해주세요.')
            if request.new_collection and not can_collect_new:
                raise HTTPException(409, '새 원문 수집 상태가 변경되었습니다. 현재 작업 상태를 다시 확인해주세요.')
            run_id = store.prepare_run(conn, profile_id, expected_revision=request.expected_revision, sites=sites)
            workflow_id = create_workflow(conn, run_id, settings=settings)
        return workflow_id, run_id
    from services.news_analysis_worker import _settle_db
    created, cancelled = await _settle_db(_execute(create))
    workflow_id, run_id = created
    if cancelled:
        from services.news_profile_workflow import cancel_if_ready
        await _settle_db(_execute(lambda conn: cancel_if_ready(conn, workflow_id)))
        raise asyncio.CancelledError
    reason = _analysis_preflight(settings, client=client, profile=profile_for_mode) if ai_required else None
    _launch_workflow(workflow_id, ownership=ownership, **_workflow_dependencies(client, settings, analysis_wait_reason=reason))
    return {'workflow_id': workflow_id, 'run_id': run_id, 'state': 'ready',
            'experimental': True}


def _collection_selection(conn, profile_id):
    """Read the current site selection without creating a run or changing settings."""
    profile = store.get_profile(conn, profile_id)
    protected, archived = conn.execute('SELECT protected,archived FROM news_profiles WHERE profile_id=?', (profile_id,)).fetchone()
    if protected or archived or profile.general_mode != 'custom_v1':
        return profile, [], '사용할 수 없는 보관 조건입니다. 대상별 조건을 새로 만들어주세요.'
    from services.news_auto_sources import retired_source_scope, SOURCE_SCOPE_CHANGED_MESSAGE
    if retired_source_scope(profile.snapshot()):
        return profile, [], SOURCE_SCOPE_CHANGED_MESSAGE
    if 'sites' in profile.sources and not profile.site_ids:
        return profile, [], '연결된 수집 사이트가 없습니다. 설정에서 이 조건의 수집 사이트를 선택해주세요.'
    sites = []
    if 'sites' in profile.sources:
        for row in conn.execute('SELECT * FROM sites ORDER BY id'):
            site = dict(row)
            if str(site['id']) in profile.site_ids:
                try:
                    site['selectors'] = json.loads(site['selectors']) if site.get('selectors') else {}
                except (ValueError, TypeError):
                    return profile, [], '수집 사이트 설정을 확인해주세요.'
                if not isinstance(site['selectors'], dict) or not all(isinstance(site.get(key), str) and site[key].strip() for key in ('name', 'url')):
                    return profile, [], '수집 사이트 설정을 확인해주세요.'
                sites.append(site)
    if 'sites' in profile.sources and {str(site['id']) for site in sites} != set(profile.site_ids):
        return profile, sites, '조건에 포함된 사이트가 삭제되었습니다. 설정에서 이 조건의 수집 사이트를 다시 선택해주세요.'
    return profile, sites, ''


def _collection_backlog(conn, profile_id, revision):
    """Only explicit new collection may coexist with finished collection/AI waits."""
    from services.news_profile_workflow import ensure_schema
    ensure_schema(conn)
    rows = conn.execute('''SELECT w.state,w.stage,w.summary_json,w.run_id FROM news_profile_workflows w
        JOIN news_run_snapshots r ON r.run_id=w.run_id WHERE r.profile_id=?
        AND (w.state IN ('ready','running') OR (w.state='waiting' AND r.revision=?))''',
        (profile_id,revision)).fetchall()
    from services.news_auto_sources import retired_source_scope
    # Old source snapshots are preserved as history, not rewritten or resumed.
    # They must not block a new sites-only collection after migration.
    rows = [row for row in rows if not retired_source_scope(
        store.get_run_snapshot(conn, row[3])['profile'])]
    def analysis_only(row):
        try:
            summary = json.loads(row[2])
            return row[0] == 'waiting' and row[1] == 'analyzing' and summary.get('collection', {}).get('state') == 'finished'
        except (ValueError, AttributeError, TypeError):
            return False
    return bool(rows), bool(rows) and all(analysis_only(row) for row in rows)


@router.get('/profiles/{profile_id}/workflow-options')
async def workflow_options(profile_id: str):
    from services import app_lifecycle
    profile, sites, selection_reason = await _execute(lambda conn: _collection_selection(conn, profile_id))
    ai_required = 'sites' in profile.sources
    client = _analysis_client() if ai_required else None
    key_ready = client is not None
    threshold_ready = _configured_semantic_threshold() is not None
    from services.news_execution_readiness import collection_readiness
    enabled, reason_code, reason = collection_readiness(
        selection_reason=selection_reason, shutting_down=app_lifecycle.shutdown_requested)
    settings = _runtime_workflow_settings() or _pending_workflow_settings()
    analysis_reason = _analysis_preflight(settings, client=client, profile=profile) if ai_required else None
    _, can_collect_new = await _execute(lambda conn: _collection_backlog(conn, profile_id, profile.revision))
    return {'enabled': enabled, 'reason_code': reason_code, 'scheduled': False,
            'ai_required': ai_required,
            'analysis_ready':not bool(analysis_reason),
            'analysis_reason_code':analysis_reason or '',
            'analysis_reason':_ANALYSIS_WAIT_MESSAGES.get(analysis_reason, ''),
            'can_start_new_collection':enabled and can_collect_new,
            'key_ready': key_ready, 'semantic_calibrated': threshold_ready,
            'analysis_mode': settings.get('mode', 'semantic') if ai_required else 'official',
            'batch_limit': None,
            'selection': {'revision': profile.revision, 'name': profile.name, 'region': profile.region,
                          'min_age_months': profile.min_age_months, 'max_age_months': profile.max_age_months,
                          'life_cycles': list(profile.life_cycles), 'characteristics': list(profile.characteristics),
                          'interest_topics': list(profile.interest_topics),
                          'publication_period': profile.publication_period,
                          'publication_start_date': profile.publication_start_date,
                          'publication_end_date': profile.publication_end_date,
                          'sources': list(profile.sources),
                          'sites': [{'id': str(site['id']), 'name': site['name']} for site in sites]},
            'reason': reason}


@router.get('/profiles/{profile_id}/workflows')
async def profile_workflows(profile_id: str, limit: int = Query(20, ge=1, le=100)):
    from services.news_profile_workflow import list_workflows
    items = await _execute(lambda conn: list_workflows(conn, profile_id, limit=limit))
    for item in items:
        item['active'] = item['workflow_id'] in _WORKFLOW_TASKS
    return {'items': items}


@router.get('/workflows/{workflow_id}')
async def workflow_status(workflow_id: str):
    from services.news_profile_workflow import get_workflow
    value = await _execute(lambda conn: get_workflow(conn, workflow_id))
    value['active'] = workflow_id in _WORKFLOW_TASKS
    return value


class ClearHistoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool
    workflow_ids: list[StrictStr] | None = Field(default=None, min_length=1, max_length=100)


@router.post('/profiles/{profile_id}/workflows/clear-history')
async def clear_workflow_history(profile_id: str, request: ClearHistoryRequest):
    from services import app_lifecycle, news_workflow_ownership, policy_sync
    from services.news_workflow_history import clear_history
    if request.confirmed is not True:
        raise HTTPException(400, '이력 정리 범위를 확인해주세요.')
    lease = news_workflow_ownership.acquire()
    if lease is None:
        raise HTTPException(409, '수집 작업이 끝난 뒤 이력을 정리해주세요.')
    locked = False
    try:
        locked = await policy_sync.acquire_db_mutation_lock('완료된 수집 이력 정리')
        if not locked:
            raise HTTPException(409, policy_sync.maintenance_busy_message())
        if app_lifecycle.shutdown_requested:
            raise HTTPException(503, '프로그램이 종료 중입니다.')
        protected = tuple(key for key, task in _WORKFLOW_TASKS.items() if not task.done())
        return await _owned_execute(lambda conn: clear_history(
            conn, profile_id, workflow_ids=request.workflow_ids, protected_ids=protected))
    finally:
        if locked:
            policy_sync.release_maintenance_lock()
        lease.close()


@router.post('/workflows/{workflow_id}/cancel')
async def cancel_workflow(workflow_id: str):
    from services.news_profile_workflow import get_workflow, cancel_if_ready
    await _execute(lambda conn: get_workflow(conn, workflow_id))
    task = _WORKFLOW_TASKS.get(workflow_id)
    if task is None or task.done():
        async def cancel_idle(_ownership):
            changed = await _owned_execute(lambda conn: cancel_if_ready(conn, workflow_id))
            return {'changed': changed, 'workflow': await _owned_execute(lambda conn: get_workflow(conn, workflow_id))}
        return await _with_workflow_ownership(cancel_idle)
    ownership = _WORKFLOW_OWNERS.get(workflow_id)
    if ownership is None:
        raise HTTPException(409, '작업 소유권을 확인할 수 없습니다. 잠시 후 다시 시도해주세요.')
    # A task cancelled before its first step cannot record its own cancellation.
    # Pin its lease through the final acknowledgement instead of releasing and
    # reacquiring across a race with another process's orphan recovery.
    with ownership.pin():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _owned_execute(lambda conn: cancel_if_ready(conn, workflow_id))
        return {'changed': True, 'workflow': await _owned_execute(lambda conn: get_workflow(conn, workflow_id))}


@router.post('/workflows/{workflow_id}/resume')
async def resume_workflow(workflow_id: str):
    return await _with_workflow_ownership(
        lambda ownership: _resume_workflow(workflow_id, ownership=ownership))


async def _resume_workflow(workflow_id: str, *, ownership, allow_initial_setup=True):
    from services.news_profile_workflow import (
        get_workflow, get_workflow_settings, prepare_resume, initialize_analysis_settings)
    if workflow_id in _WORKFLOW_TASKS:
        raise HTTPException(409, '이 작업은 이미 진행 중입니다.')
    current, saved, snapshot = await _owned_execute(lambda conn: (
        get_workflow(conn, workflow_id), get_workflow_settings(conn, workflow_id),
        store.get_run_snapshot(conn, get_workflow(conn, workflow_id)['run_id'])))
    from services.news_auto_sources import (
        retired_source_scope, SOURCE_SCOPE_CHANGED_REASON, SOURCE_SCOPE_CHANGED_MESSAGE)
    if retired_source_scope(snapshot['profile']):
        raise HTTPException(409, detail={'error': SOURCE_SCOPE_CHANGED_REASON,
                                       'message': SOURCE_SCOPE_CHANGED_MESSAGE})
    if not current['can_resume']:
        raise HTTPException(409, '잠시 멈춘 작업만 다시 이어서 진행할 수 있습니다.')
    collecting = current['stage'] in ('prepared', 'collecting')
    ai_required = 'sites' in snapshot['profile']['sources']
    if not saved or type(saved.get('headless')) is not bool:
        raise HTTPException(409, '처음 실행할 때 사용한 분석 설정을 확인해주세요.')
    profile_values = dict(snapshot['profile'])
    profile_values.pop('schema_version')
    profile = NewsProfile(**profile_values)
    client = _analysis_client() if ai_required else None
    initial_settings = None
    if saved.get('mode') == 'analysis-setup-required-v1' and not collecting:
        # First model/recipe binding is user-driven, not a scheduled side effect.
        if not allow_initial_setup:
            raise HTTPException(409, _ANALYSIS_WAIT_MESSAGES['analysis_settings_required'])
        initial_settings = _runtime_workflow_settings()
        if initial_settings is None:
            raise HTTPException(409, _ANALYSIS_WAIT_MESSAGES['analysis_settings_required'])
        initial_settings['headless'] = saved['headless']
    active_settings = initial_settings or saved
    if ai_required:
        reason = _analysis_preflight(active_settings, client=client, profile=profile)
    else:
        if saved != _offline_workflow_settings(headless=saved['headless']):
            raise HTTPException(409, '처음 실행할 때 사용한 분석 설정을 확인해주세요.')
        reason = None
    if reason and not collecting:
        raise HTTPException(409, _ANALYSIS_WAIT_MESSAGES[reason])
    # Build/validate dependencies before changing jobs. Collection-phase resumes
    # use the original saved recipe even when the current AI setup has changed.
    dependencies = _workflow_dependencies(client, active_settings, analysis_wait_reason=reason)
    def prepare(conn):
        with store._atomic(conn):
            if initial_settings is not None:
                initialize_analysis_settings(conn, workflow_id, expected=saved, settings=initial_settings)
            prepare_resume(conn, workflow_id, resume_analysis=not collecting)
    await _owned_execute(prepare)
    _launch_workflow(workflow_id, ownership=ownership, **dependencies)
    return {'workflow_id': workflow_id, 'state': 'waiting', 'scheduled': True}

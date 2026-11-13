"""v19 full-input news transport with resumable, lossless range partitions.

One source that fits is analyzed once. Larger sources retain every character in
paragraph-oriented parts. Parts are NOT independent eligibility decisions:
multi-part output is always held for cross-part review, never auto-approved or
auto-excluded. A saved part is only a private checkpoint, not a news item.
"""
from copy import deepcopy
import hashlib
import json
import logging

from services import ai_coordinator, news_contact_input, news_source_selection as selection
from services import news_custom_analysis as custom, page_coverage, news_ai_input
from services import news_readable_content as readable
from services.news_custom_request_plan import build_request, compact_response_schema

logger = logging.getLogger(__name__)


def _identity(profile, payload, model, *, recipe=custom.COMPLETE_RECIPE):
    encoded = json.dumps([recipe, profile.fingerprint, payload, model],
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _split(source, start, end):
    """Keep all characters, prefer a paragraph/line boundary near the midpoint."""
    middle = (start + end) // 2
    lower, upper = start + (end - start) // 4, end - (end - start) // 4
    for separator in ('\n\n', '\n', '. ', ' '):
        positions = []
        offset = source.find(separator, lower, upper)
        while offset >= 0:
            cut = offset + len(separator)
            if source[start:cut].strip() and source[cut:end].strip():
                positions.append(cut)
            offset = source.find(separator, offset + len(separator), upper)
        if positions:
            middle = min(positions, key=lambda value: abs(value - middle))
            break
    if not start < middle < end:
        raise ai_coordinator.AiInputTokenBudgetExceeded('분석 지침만으로 입력 보호값에 도달했습니다.')
    return [[start, middle], [middle, end]]


def _validate_part(raw, text, *, readable_content=False):
    from services.news_classification_result import validate_result
    canonical = dict(raw) if isinstance(raw, dict) else raw
    if readable_content and isinstance(canonical, dict):
        canonical.pop(readable.RESPONSE_KEY, None)
    materialized = selection.materialize(canonical, text, extended=True, complete_document=True)
    validate_result(materialized)
    custom.validate_facts(materialized)
    if readable_content:
        readable.attach(materialized, raw.get(readable.RESPONSE_KEY))
        # Do not persist unbounded or malformed optional model metadata in a
        # resumable checkpoint. A missing display never invalidates facts.
        return dict(canonical, **{readable.RESPONSE_KEY:
            materialized.get(readable.META_KEY, {}).get('sections', [])})
    return raw


def _checkpoint(value, identity, source, *, readable_content=False):
    if value is None:
        return dict(identity=identity, done=[], pending=[[0, len(source)]])
    if (not isinstance(value, dict) or set(value) != {'identity', 'done', 'pending'}
            or value['identity'] != identity or not isinstance(value['done'], list)
            or not isinstance(value['pending'], list)
            or not 1 <= len(value['done']) + len(value['pending']) <= 64):
        raise ValueError('Invalid source checkpoint')
    ranges = []
    for entry in value['done']:
        if not isinstance(entry, dict) or set(entry) != {'span', 'result'}:
            raise ValueError('Invalid source checkpoint part')
        ranges.append(entry['span'])
    ranges += value['pending']
    if any(not isinstance(span, list) or len(span) != 2
           or any(type(bound) is not int for bound in span)
           or not 0 <= span[0] < span[1] <= len(source) for span in ranges):
        raise ValueError('Invalid checkpoint bounds')
    cursor = 0
    for start, end in sorted(ranges):
        if start != cursor:
            raise ValueError('Checkpoint has missing or overlapping source')
        cursor = end
    if cursor != len(source):
        raise ValueError('Checkpoint omits source tail')
    for part in value['done']:
        start, end = part['span']
        _validate_part(part['result'], source[start:end], readable_content=readable_content)
    return deepcopy(value)


def _merge(parts):
    """No concatenated paraphrases, inferred ranges or mixed applicant routes."""
    if len(parts) == 1:
        return deepcopy(parts[0])
    result = {}
    for key in parts[0]:
        values = [part[key] for part in parts]
        # Nonempty values must agree. Conflicting facts stay unknown; even
        # agreement cannot lift the mandatory multi-part review gate.
        present = [value for value in values if value not in (None, '', [], {}, '미상', 'unknown')]
        if key == 'fact_evidence':
            result[key] = {field: '' for field in values[0]}
        elif key == '_support_source_units':
            result[key] = [1] if all(value == [1] for value in values) else []
        elif key == 'is_relevant':
            result[key] = any(values)  # mandatory review prevents a false exclusion
        elif present and all(value == present[0] for value in present):
            result[key] = deepcopy(present[0])
        else:
            result[key] = ([] if isinstance(values[0], list) else None
                if key in ('start_age_months', 'end_age_months') else 'unknown'
                if key == 'applicant_type' else '미상'
                if key in ('category', 'target', 'applicant_detail', 'beneficiary_detail') else '')
    for field in result['fact_evidence']:
        quotes = [part['fact_evidence'][field] for part in parts if part['fact_evidence'][field]]
        if quotes and all(quote == quotes[0] for quote in quotes):
            result['fact_evidence'][field] = quotes[0]
    return result


async def classify_complete(client, profile, payload, model, *, checkpoint=None, save_checkpoint=None,
                            ensure_active=None, recipe=custom.COMPLETE_RECIPE):
    import ai_classifier
    from services.ai_verified_input import verify_input
    if profile.general_mode != 'custom_v1' or not client:
        raise ValueError('Custom profile and client required')
    if recipe not in (custom.COMPLETE_RECIPE, custom.READABLE_RECIPE):
        raise ValueError('Unsupported complete-source recipe')
    readable_content = recipe == custom.READABLE_RECIPE
    criteria = {key: getattr(profile, key) for key in (
        'region', 'min_age_months', 'max_age_months', 'life_cycles', 'characteristics', 'interest_topics')}
    # Even token counting sends text externally. Inspect the ENTIRE original
    # before any range selection, cache lookup, count or generation request.
    transmission, contact = news_contact_input.ensure_news_payload_safe(
        payload, json.dumps(criteria, ensure_ascii=False), profile.ai_prompt)
    original, content = payload['content'], transmission['content']
    if not selection.eligible_source(original, complete_document=True):
        if isinstance(original, str) and len(original) > selection.COMPLETE_SOURCE_CHARS:
            raise ai_coordinator.AiInputTokenBudgetExceeded(
                '원문이 전체 본문 분석의 보존 한도를 초과했습니다.')
        raise selection.SourceInputReviewRequired(
            '원문 범위를 안전하게 확인하지 못했습니다. 생략 표시·본문 형식과 상세 출처를 확인해주세요.')
    identity = _identity(profile, payload, model, recipe=recipe)
    state = _checkpoint(checkpoint, identity, content, readable_content=readable_content)
    schema = selection.response_schema(custom.response_schema(complete_grounding=True))
    if readable_content:
        schema = readable.response_schema(schema)
    schema = compact_response_schema(schema)
    page = page_coverage.normalize(payload.get('source_page'))

    async def persist():
        if save_checkpoint is not None:
            await save_checkpoint(state)

    while state['pending']:
        if ensure_active is not None:
            await ensure_active()
        start, end = state['pending'][0]
        text = content[start:end]
        source = {key: payload[key] for key in ('title', 'link', 'site_name')}
        source['content'] = text
        budget = ai_coordinator.input_token_capacity(model)
        if budget <= 0:
            raise ai_coordinator.AiQuotaStateUnavailable('AI 전송 보호값을 확인하지 못했습니다. 잠시 후 이어서 진행해주세요.')
        prompt = build_request(criteria, profile.ai_prompt, source, schema,
            coverage={'status': 'complete'}, input_token_budget=budget, whole_source=True,
            contact_metadata=contact, page_state=page, defer_token_check=True)
        if readable_content:
            prompt = readable.PROMPT + prompt
        if start != 0 or end != len(content):
            prompt = ('This is one consecutive part of the SAME announcement. Unknown facts in this part '
                      'are NOT absence of conditions. Do not decide eligibility of the whole document.\n') + prompt
        verified = await verify_input(client, prompt, schema, model=model, input_token_budget=budget,
                                      before_dispatch=ensure_active)
        if ensure_active is not None:
            await ensure_active()
        # Numeric planning diagnostics only: never article titles/text/URLs,
        # custom preferences, provider errors or credentials.
        logger.info('news_input_plan chars=%d total_chars=%d input_allowance=%d estimated_tokens=%d counted=%s completed_parts=%d',
                    len(text), len(content), budget, verified.input_tokens,
                    verified.is_provider_counted, len(state['done']))
        if verified.input_tokens > budget:
            if verified.method != 'provider_prompt_plus_schema_bytes':
                # Never spend many generation calls on tiny byte-estimated
                # pieces just because the tokenizer is temporarily unavailable.
                raise ai_coordinator.AiQuotaStateUnavailable('입력 크기를 확인하지 못했습니다. 원문을 보존하고 재시도합니다.')
            if end - start <= 256 or len(state['done']) + len(state['pending']) >= 64:
                raise ai_coordinator.AiInputTokenBudgetExceeded('분석 지침과 응답 형식이 현재 AI 입력 보호값보다 큽니다.')
            state['pending'][:1] = _split(content, start, end)
            await persist()
            continue
        raw = await ai_classifier._call_gemini_json(client, prompt, payload['title'],
            model_override=model, response_json_schema=schema, request_attempts=1,
            verified_input=verified, before_dispatch=ensure_active)
        raw = _validate_part(raw, text, readable_content=readable_content)
        state['done'].append(dict(span=[start, end], result=raw))
        state['pending'].pop(0)
        await persist()
    state = _checkpoint(state, identity, content, readable_content=readable_content)
    parts = [part['result'] for part in sorted(state['done'], key=lambda part: part['span'][0])]
    # Optional presentation must never broaden or invalidate classification.
    # Part-local passages cannot establish cross-part condition relationships.
    sections = parts[0].get(readable.RESPONSE_KEY) if readable_content and len(parts) == 1 else None
    canonical_parts = [dict(part) for part in parts]
    if readable_content:
        for part in canonical_parts:
            part.pop(readable.RESPONSE_KEY, None)
    result = selection.materialize(_merge(canonical_parts), original, extended=True,
                                  complete_document=True, part_count=len(parts))
    if readable_content and len(parts) == 1:
        readable.attach(result, sections)
    news_contact_input.clear_model_contact(result, contact)
    _, coverage = news_ai_input.prepare(content, selection.COMPLETE_SOURCE_CHARS)
    result['source_input'] = news_contact_input.add_coverage_metadata(coverage, contact)
    if page:
        result['source_page'] = page
    return result

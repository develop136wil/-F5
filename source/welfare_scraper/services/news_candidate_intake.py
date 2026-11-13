"""Opt-in site candidate intake. No fetching, AI calls, or legacy item writes.

Title matches and explicit body target clues are queued for analysis, not accepted
as eligible results. Semantic candidates are durably retained
until a profile-aware semantic gate is connected, not silently excluded.
"""
import hashlib
import ipaddress
import json
from urllib.parse import urlsplit

from services import news_analysis_store as jobs
from services.news_analysis_context import NewsAnalysisContext
from services.news_analysis_worker import RECIPE as LEGACY_RECIPE
from services.news_custom_analysis import RECIPE as CUSTOM_RECIPE


def ensure_schema(conn):
    jobs.ensure_schema(conn)


def validate_candidate(candidate):
    if (not isinstance(candidate, dict) or not {'title', 'link', 'content'} <= set(candidate)
            or set(candidate) - {'title', 'link', 'content', 'source_page', 'published_date',
                                'publication_status', 'selection_reason'}):
        raise ValueError('Expected exact title/link/content')
    if any(not isinstance(candidate[key], str) for key in ('title', 'link', 'content')):
        raise ValueError('Candidate fields must be strings')
    if 'published_date' in candidate or 'publication_status' in candidate:
        from services.news_publication import valid_iso
        published = candidate.get('published_date')
        if ((published is not None and not valid_iso(published))
                or candidate.get('publication_status') != ('known' if published else 'unknown')):
            raise ValueError('Invalid publication date metadata')
    if candidate.get('selection_reason', '') not in ('', 'outside_publication_period', 'not_article'):
        raise ValueError('Invalid candidate selection reason')
    if 'source_page' in candidate:
        from services.page_coverage import normalize
        if not isinstance(candidate['source_page'], dict) or candidate['source_page'] != normalize(candidate['source_page']):
            raise ValueError('Invalid page coverage')
    if not candidate['title'].strip() or len(candidate['title']) > 2000 or len(candidate['content']) > 1000000:
        raise ValueError('Invalid candidate size or title')
    link = urlsplit(candidate['link'])
    if len(candidate['link']) > 10000 or link.scheme not in ('http', 'https') or not link.hostname or link.username or link.password:
        raise ValueError('Expected HTTP(S) link without credentials')
    hostname = link.hostname.rstrip('.').lower()
    if hostname == 'localhost' or hostname.endswith(('.localhost', '.local')):
        raise ValueError('Local source links are not allowed')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('Private or reserved source links are not allowed')


def register_candidates(conn, run_id, site_id, candidates, *, model, defer_analysis=False):
    """Atomically register a bounded batch against the original run criteria.

The caller must pass collected title/link/content strings. Site identity/name
comes from the run snapshot; mutable current settings are never read.
"""
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Explicit model required')
    if type(defer_analysis) is not bool:
        raise ValueError('Explicit deferred analysis mode required')
    if not isinstance(candidates, (list, tuple)) or not 1 <= len(candidates) <= 100:
        raise ValueError('Expected 1..100 candidates')
    with jobs._candidate_batch(conn, run_id) as (snapshot, profile, enqueue_candidate):
        context = NewsAnalysisContext(profile)
        site = next((s for s in snapshot['sites'] if str(s['id']) == str(site_id)), None)
        if site is None:
            raise ValueError('Site is not selected in this run')
        recipe = CUSTOM_RECIPE if context.profile.general_mode == 'custom_v1' else LEGACY_RECIPE
        ensure_schema(conn)
        results = []
        for candidate in candidates:
            validate_candidate(candidate)
            payload = dict(candidate, site_name=site['name'])
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            key = hashlib.sha256((str(site['id']) + '\n' + encoded).encode()).hexdigest()
            existing = conn.execute('SELECT route,job_id FROM news_run_candidates WHERE run_id=? AND candidate_key=?',
                                    (run_id, key)).fetchone()
            if existing:
                results.append(dict(candidate_key=key, route=existing[0], job_id=existing[1], created=False))
                continue
            route, _ = context.route_candidate(candidate['title'], candidate['content'])
            if candidate.get('selection_reason'):
                route = 'excluded'
            if defer_analysis and route != 'excluded':
                route = 'analysis_unconfigured'
            # Selection/display metadata is not an AI instruction and must not
            # change the exact content input or existing analysis cache keys.
            from services.news_publication import analysis_payload
            analysis_input = analysis_payload(payload)
            job_id = enqueue_candidate(payload=analysis_input, model=model, recipe=recipe) if route == 'matched' else None
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
                         (run_id, key, str(site['id']), encoded, route, job_id))
            results.append(dict(candidate_key=key, route=route, job_id=job_id, created=True))
        return results

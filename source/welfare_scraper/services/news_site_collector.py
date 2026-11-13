"""Collect configured site-page scope into durable, restart-safe candidate batches.

This deliberately bypasses legacy keyword/age filters and welfare_items writes.
The production default processes all valid returned items, not a preview sample.
"""
import asyncio
import hashlib
import json
import aiohttp

from services import app_lifecycle
from services.news_profile_store import get_run_snapshot, _atomic
from services.news_analysis_worker import db_call, _settle_db
from services.news_candidate_intake import register_candidates, validate_candidate, ensure_schema
from services import news_attachments
from services import news_publication as publication
from services.news_legacy_recovery import identity_url


_SAVE_BATCH_SIZE = 20
_VISION_REVIEW = 'vision_review_required'
_CONTENT_CONCURRENCY = 2
_BROWSER_FAILURE_LIMIT = 3


def _needs_browser(page):
    """Render only a failed/empty document, never a rate limit or login wall."""
    if not isinstance(page, dict):
        return True
    if page.get('fetch_failure_kind') in ('rate_limited', 'not_found', 'unauthorized', 'unsafe_url'):
        return False
    if page.get('images') or page.get('attachments') or page.get('attachment_items'):
        return False  # Existing local attachment OCR handles image announcements.
    return len(str(page.get('text') or '').strip()) < 80


def _needs_poster_capture(page):
    """A failed download of an inline poster, not another OCR of a readable file."""
    if (len(str(page.get('text') or '').strip()) >= 80
            or str(page.get('attachment_text') or '').strip()):
        return False
    images = page.get('images') or []
    return any(isinstance(report, dict) and report.get('url') in images
               and report.get('kind') in ('image', 'png', 'jpg', 'jpeg', 'webp')
               and report.get('status') == 'download_failed'
               for report in (page.get('attachment_reports') or []))


async def _read_candidate(session, candidate, fetcher, recover, publication_window=None):
    """HTTP, bounded browser reading and local extraction; no provider calls."""
    if app_lifecycle.shutdown_requested:
        return None
    metrics = {}
    content = ''
    has_evidence = False
    try:
        try:
            page = await fetcher(session, candidate['link'])
        except asyncio.CancelledError:
            raise
        except Exception:
            page = None
        if _needs_browser(page):
            restored = await recover(candidate['link'])
            if isinstance(restored, dict) and (str(restored.get('text') or '').strip()
                    or restored.get('images') or restored.get('attachment_items')):
                page = restored
                metrics['browser_recovered'] = 1
        if isinstance(page, dict):
            # Read an explicit original-page publication label before expensive
            # attachment OCR. Deadline and crawl dates are not publication dates.
            published = candidate.get('published_date') or publication.from_labelled_text(page.get('text'))
            publication.attach(candidate, published)
            if publication.outside_window(published, publication_window):
                candidate['selection_reason'] = 'outside_publication_period'
                candidate['content'] = str(page.get('text') or '')[:15000]
                return candidate, {'outside_publication_period': 1}
            try:
                page = await news_attachments.enrich_page_locally(session, page)
            except asyncio.CancelledError:
                raise
            except Exception:
                # An attachment failure must not discard a successfully read body.
                page = dict(page, attachment_notices=['첨부파일을 확인하지 못했습니다.'])
            from services.page_coverage import normalize, with_methods, with_reasons
            if _needs_poster_capture(page):
                restored = await recover(candidate['link'], capture_images=True)
                restored_text = str((restored or {}).get('text') or '').strip()
                if restored_text:
                    original = str(page.get('text') or '').strip()
                    merged = restored_text if original in restored_text else original + '\n' + restored_text
                    page = dict(page, text=merged[:15000])
                    source = normalize((restored or {}).get('source_page'))
                    page['source_page'] = with_reasons(page.get('source_page'),
                        *source.get('reasons', []), *(['page_char_limit'] if len(merged) > 15000 else []))
                    page['source_page'] = with_methods(page['source_page'], *source.get('methods', []))
                    metrics['browser_recovered'] = 1
                    # Keep original failed-download/partial-coverage notices. Do
                    # not claim the file was fully read or download it a second time.
            candidate['source_page'] = normalize(page.get('source_page'))
            content = news_attachments.merge_page_text(page)
            has_evidence = bool(str(page.get('text') or '').strip()
                                or str(page.get('attachment_text') or '').strip())
            attachments = news_attachments.summarize_attachment_reports(page.get('attachment_reports'))
            if any(report.get('status') == 'ocr_text_extracted' for report in attachments['files']):
                candidate['source_page'] = with_methods(candidate['source_page'], 'local_ocr')
            if attachments['count']:
                metrics['attachments_analyzed'] = attachments['analyzed']
                metrics['attachment_review'] = attachments['count'] - attachments['analyzed']
                if attachments['cache_hits']:
                    metrics['attachments_reused'] = attachments['cache_hits']
            if attachments['analyzed'] < attachments['count'] or page.get('attachment_notices'):
                candidate['source_page'] = with_reasons(candidate['source_page'], 'attachment_review')
    except asyncio.CancelledError:
        raise
    except Exception:
        content = ''
        has_evidence = False
    if not has_evidence:
        metrics['content_failed'] = 1
    if 'source_page' not in candidate:
        from services.page_coverage import make
        candidate['source_page'] = make('fetch_failed')
    candidate['content'] = content if isinstance(content, str) else ''
    publication.attach(candidate, candidate.get('published_date'))
    return candidate, metrics


def _ensure_issue_schema(conn):
    with _atomic(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS news_collection_issues (
            run_id TEXT NOT NULL, site_id TEXT NOT NULL, issue_key TEXT NOT NULL,
            PRIMARY KEY(run_id, site_id, issue_key),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id) ON DELETE CASCADE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_collection_site_issues (
            run_id TEXT NOT NULL, site_id TEXT NOT NULL, issue_code TEXT NOT NULL,
            PRIMARY KEY(run_id, site_id),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id) ON DELETE CASCADE)''')


def _site_issues(conn, run_id):
    return dict(conn.execute('SELECT site_id,issue_code FROM news_collection_site_issues WHERE run_id=?',
                             (run_id,)))


def _record_site_outcome(conn, run_id, site_id, issue_code):
    """Persist only a safe status, not remote exception text or page contents.

    Unlike an unread captured document, a list-reading error can be cleared by
    a later successful read of the same site during an interrupted run.
    """
    with _atomic(conn):
        if issue_code is None:
            conn.execute('DELETE FROM news_collection_site_issues WHERE run_id=? AND site_id=?',
                         (run_id, site_id))
        else:
            conn.execute('''INSERT INTO news_collection_site_issues VALUES (?,?,?)
                ON CONFLICT(run_id,site_id) DO UPDATE SET issue_code=excluded.issue_code''',
                         (run_id, site_id, issue_code))
        return _site_issues(conn, run_id)


def _with_site_issues(summary, issues, sites):
    for key in ('site_issues', 'site_failed', 'site_partial', 'site_review_required'):
        summary.pop(key, None)
    if not issues:
        return
    names = {str(site['id']): ' '.join(str(site.get('name') or '등록 사이트').split())[:120]
             for site in sites}
    summary['site_issues'] = [dict(site_id=site_id, site_name=names.get(site_id, '등록 사이트'),
                                 issue_code=code) for site_id, code in issues.items()]
    summary['site_failed'] = sum(code != 'site_partial' for code in issues.values())
    summary['site_partial'] = sum(code == 'site_partial' for code in issues.values())
    summary['site_review_required'] = len(issues)


def _unusable_identity(item):
    # Retain only a digest, never unsafe URLs, malformed remote data or titles.
    fields = [item.get(key) for key in ('title', 'link')] if isinstance(item, dict) else [item]
    fields = [value.strip() if isinstance(value, str) else '<' + type(value).__name__ + '>'
              for value in fields]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def _record_issues(conn, run_id, site_id, keys):
    with _atomic(conn):
        _ensure_issue_schema(conn)
        conn.executemany('INSERT OR IGNORE INTO news_collection_issues VALUES (?,?,?)',
                         [(run_id, site_id, key) for key in keys])
        return _issue_counts(conn, run_id)


def _issue_counts(conn, run_id):
    # Missing links count documents; unreviewed screen images count sites.
    # Keep these separate and persist no screenshot, URL or sensitive text.
    return conn.execute('''SELECT COUNT(CASE WHEN issue_key<>? THEN 1 END),
        COUNT(CASE WHEN issue_key=? THEN 1 END) FROM news_collection_issues WHERE run_id=?''',
        (_VISION_REVIEW, _VISION_REVIEW, run_id)).fetchone()


def _registered_progress(conn, run_id):
    """Existing candidate receipts are also the same-run collection checkpoint.

    Do not refetch saved identities on resume: mutable body text would otherwise
    change the content hash and create a second job for the same captured item.
    """
    ensure_schema(conn)
    _ensure_issue_schema(conn)
    identities = {}
    total = 0
    # Receipts need identity only. Keep potentially large source bodies in SQLite
    # instead of transferring and decoding all of them again on every resume.
    for site_id, title, link in conn.execute('''SELECT site_id,
            CASE WHEN json_type(payload_json,'$.title')='text' THEN json_extract(payload_json,'$.title') END,
            CASE WHEN json_type(payload_json,'$.link')='text' THEN json_extract(payload_json,'$.link') END
            FROM news_run_candidates WHERE run_id=?''', (run_id,)):
        if not isinstance(title, str) or not isinstance(link, str):
            raise ValueError('Invalid saved candidate identity')
        identity = identity_url(link)
        if not identity:
            raise ValueError('Invalid saved candidate identity')
        identities.setdefault(str(site_id), set()).add(identity)
        total += 1
    skipped, vision_review = _issue_counts(conn, run_id)
    return identities, total, skipped, vision_review, _site_issues(conn, run_id)


def _previous_links(conn, run_id):
    """Prioritize unseen URLs, but still re-read known URLs to detect changes.

    Never use a previous run's start/end as an exclusion checkpoint: interrupted
    collection or edited old posts would otherwise be lost.
    """
    ensure_schema(conn)
    rows = conn.execute('''SELECT CASE WHEN json_type(c.payload_json,'$.link')='text'
            THEN json_extract(c.payload_json,'$.link') END FROM news_run_candidates c
        JOIN news_run_snapshots r ON r.run_id=c.run_id
        WHERE r.profile_id=(SELECT profile_id FROM news_run_snapshots WHERE run_id=?)
          AND c.run_id<>? ORDER BY c.rowid DESC LIMIT 5000''', (run_id, run_id))
    links = set()
    for (link,) in rows:
        if not isinstance(link, str):
            raise ValueError('Invalid saved candidate link')
        links.add(link)
    return links


async def collect_registered_sites(run_id, *, analysis_model, headless=True, limit_per_site=None,
                                   call_db=db_call, engine_factory=None, content_fetcher=None,
                                   defer_analysis=False, progress=None):
    if (type(defer_analysis) is not bool or not isinstance(analysis_model, str) or not analysis_model.strip()
            or (limit_per_site is not None
                and (type(limit_per_site) is not int or not 1 <= limit_per_site <= 100))):
        raise ValueError('Invalid collection settings')
    snapshot = await call_db(get_run_snapshot, run_id)
    publication_window = publication.window_from_snapshot(snapshot)
    previous_links = await call_db(_previous_links, run_id)
    from services.news_analysis_context import NewsAnalysisContext
    from services.news_profile import NewsProfile
    captured_profile = dict(snapshot['profile'])
    captured_profile.pop('schema_version')
    context = NewsAnalysisContext(NewsProfile(**captured_profile))
    persisted, saved_total, skipped, vision_review, site_issues = await call_db(_registered_progress, run_id)
    summary = {'state': 'finished', 'sites': 0, 'registered': 0, 'content_failed': 0,
               'saved_total': saved_total}
    # A resumed collection rechecks site lists, but skips previously saved
    # bodies. These counters describe this collection attempt, not imagined
    # completed sites inferred from existing candidate counts.
    from services.news_workflow_progress import initial_progress
    live = initial_progress(len(snapshot['sites']))

    async def emit_progress(*, phase=None, force=False):
        if progress is None:
            return
        if phase is not None:
            live['phase'] = phase
        live.update(saved_candidates=summary['saved_total'],
                    attachments_processed=summary.get('attachments_analyzed', 0) + summary.get('attachment_review', 0),
                    attachments_review_required=summary.get('attachment_review', 0))
        await progress(live, force=force)

    await emit_progress(force=True)
    if skipped:
        summary['skipped_candidates'] = skipped
    if vision_review:
        summary['vision_review_sites'] = vision_review
    _with_site_issues(summary, site_issues, snapshot['sites'])
    if not snapshot['sites']:
        return summary
    if app_lifecycle.shutdown_requested:
        return dict(summary, state='shutdown')

    from services.scraper_service import _acquire_scrape_lock, _release_scrape_lock
    handle, cancelled = await _settle_db(asyncio.to_thread(_acquire_scrape_lock))
    if cancelled:
        if handle is not None:
            _release_scrape_lock(handle)
        raise asyncio.CancelledError
    if handle is None:
        return dict(summary, state='busy')
    if app_lifecycle.shutdown_requested:
        _release_scrape_lock(handle)
        return dict(summary, state='shutdown')
    engine = None
    candidates = []
    skipped_keys = set()
    site_id = None
    save_failed = False
    browser_lock = asyncio.Lock()
    browser_failures = 0

    async def recover(url, *, capture_images=False):
        nonlocal browser_failures
        render = getattr(engine, 'fetch_rendered_page', None)
        if not callable(render):
            return None
        async with browser_lock:
            if app_lifecycle.shutdown_requested or browser_failures >= _BROWSER_FAILURE_LIMIT:
                return None
            try:
                # Includes optional local canvas OCR; never an external Vision call.
                restored = await asyncio.wait_for(render(url,
                    **({'capture_images': True} if capture_images else {})), timeout=27)
            except asyncio.CancelledError:
                raise
            except Exception:
                restored = None
            useful = isinstance(restored, dict) and (str(restored.get('text') or '').strip()
                or restored.get('images') or restored.get('attachment_items'))
            if useful and capture_images:
                useful = ('local_ocr' in (restored.get('source_page') or {}).get('methods', [])
                          or len(str(restored.get('text') or '').strip()) >= 80)
            browser_failures = 0 if useful else browser_failures + 1
            return restored if useful else None

    def accept(result):
        if result is None:
            return
        candidate, metrics = result
        candidates.append(candidate)
        for key, value in metrics.items():
            summary[key] = summary.get(key, 0) + value

    async def read_bounded(session, selected):
        """Two reads maximum; DB writes remain serial and completed work survives cancel."""
        remaining = iter(selected)
        pending = set()
        try:
            while True:
                while len(pending) < _CONTENT_CONCURRENCY and not app_lifecycle.shutdown_requested:
                    candidate = next(remaining, None)
                    if candidate is None:
                        break
                    pending.add(asyncio.create_task(_read_candidate(session, candidate, content_fetcher, recover,
                                                                    publication_window)))
                if not pending:
                    break
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    pending.remove(task)
                    result = task.result()
                    accept(result)
                    if result is not None:
                        live['site_documents_processed'] += 1
                        live['documents_processed'] += 1
                    if len(candidates) >= _SAVE_BATCH_SIZE:
                        await flush()
                    await emit_progress()
                if app_lifecycle.shutdown_requested:
                    summary['state'] = 'shutdown'
                    break
        finally:
            # Harvest already completed reads even if cancellation arrived before
            # asyncio.wait returned. Unfinished reads can safely resume next time.
            for task in tuple(pending):
                if task.done():
                    pending.remove(task)
                    if not task.cancelled() and task.exception() is None and not save_failed:
                        result = task.result()
                        accept(result)
                        if result is not None:
                            live['site_documents_processed'] += 1
                            live['documents_processed'] += 1
                else:
                    task.cancel()
            if pending:
                drain = asyncio.gather(*pending, return_exceptions=True)
                cancelled = False
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        cancelled = True
                drain.result()
                if cancelled:
                    raise asyncio.CancelledError

    async def record_outcome(issue_code):
        nonlocal save_failed
        try:
            issues, cancelled = await _settle_db(call_db(
                _record_site_outcome, run_id, site_id, issue_code))
        except BaseException:
            save_failed = True
            raise
        _with_site_issues(summary, issues, snapshot['sites'])
        if cancelled:
            raise asyncio.CancelledError
        live['sites_processed'] += 1
        if issue_code is None and not site_vision_review:
            live['sites_succeeded'] += 1
        else:
            live['sites_review_required'] += 1
        await emit_progress(phase='sites', force=True)

    async def flush():
        nonlocal save_failed
        if skipped_keys:
            try:
                counts, cancelled = await _settle_db(call_db(
                    _record_issues, run_id, site_id, tuple(skipped_keys)))
            except BaseException:
                save_failed = True
                raise
            skipped, vision_review = counts
            if skipped:
                summary['skipped_candidates'] = skipped
            if vision_review:
                summary['vision_review_sites'] = vision_review
            skipped_keys.clear()
            if cancelled:
                raise asyncio.CancelledError
        if not candidates:
            return
        try:
            registered, cancelled = await _settle_db(call_db(register_candidates,
                run_id, site_id, candidates, model=analysis_model,
                **({'defer_analysis':True} if defer_analysis else {})))
        except BaseException:
            # Do not automatically retry a failed write during exception cleanup.
            save_failed = True
            raise
        added = sum(item['created'] for item in registered)
        summary['registered'] += added
        summary['saved_total'] += added
        persisted.setdefault(site_id, set()).update(identity_url(item['link']) for item in candidates)
        candidates.clear()
        if cancelled:
            raise asyncio.CancelledError

    try:
        if engine_factory is None:
            from scraper_engine import ScraperEngine
            engine_factory = ScraperEngine
        engine = engine_factory(headless=headless)
        await engine.start()
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4, limit_per_host=2)) as session:
            if content_fetcher is None:
                from ai_classifier import fetch_page_content_async
                async def content_fetcher(session, url):
                    return await fetch_page_content_async(session, url,
                        url_validator=news_attachments.validate_public_attachment_url)
            for index, site in enumerate(snapshot['sites'], 1):
                if app_lifecycle.shutdown_requested:
                    summary['state'] = 'shutdown'
                    break
                site_id = str(site['id'])
                browser_failures = 0
                site_vision_review = False
                live.update(site_index=index, site_name=site.get('name') or '등록 사이트',
                            site_documents_total=None, site_documents_processed=0)
                await emit_progress(phase='sites', force=True)
                try:
                    # The collector reads the body once below; do not prefetch it
                    # just to replace an already extracted list title.
                    outcome = await engine.scrape_site(dict(site, fallback_enrich_titles=False, news_article_only=True),
                        known_links=set(), with_status=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await record_outcome('site_read_failed')
                    continue
                if (not isinstance(outcome, dict) or outcome.get('state') not in ('finished','partial','failed')
                        or not isinstance(outcome.get('items'), (list, tuple))):
                    await record_outcome('site_response_invalid')
                    continue
                raw = outcome['items']
                if outcome.get('vision_review_required') is True:
                    site_vision_review = True
                    skipped_keys.add(_VISION_REVIEW)
                seen = set()
                selected = 0
                to_read = []
                for item in raw:
                    if app_lifecycle.shutdown_requested:
                        summary['state'] = 'shutdown'
                        break
                    if not isinstance(item, dict) or not isinstance(item.get('title'), str) or not isinstance(item.get('link'), str):
                        skipped_keys.add(_unusable_identity(item))
                        if len(skipped_keys) >= _SAVE_BATCH_SIZE:
                            await flush()
                        continue
                    candidate = {'title': item['title'], 'link': item['link'], 'content': ''}
                    publication.attach(candidate, publication.from_list_item(item))
                    try:
                        validate_candidate(candidate)
                    except ValueError:
                        skipped_keys.add(_unusable_identity(item))
                        if len(skipped_keys) >= _SAVE_BATCH_SIZE:
                            await flush()
                        continue
                    identity = identity_url(item['link'])
                    if not identity:
                        skipped_keys.add(_unusable_identity(item))
                        if len(skipped_keys) >= _SAVE_BATCH_SIZE:
                            await flush()
                        continue
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if limit_per_site is not None and selected >= limit_per_site:
                        summary.update(sampled=True, sample_limit=limit_per_site)
                        break
                    selected += 1
                    if identity in persisted.get(site_id, set()):
                        continue
                    if publication.outside_window(candidate['published_date'], publication_window):
                        candidate['selection_reason'] = 'outside_publication_period'
                        accept((candidate, {'outside_publication_period': 1}))
                        if len(candidates) >= _SAVE_BATCH_SIZE:
                            await flush()
                        continue
                    if context.route_title(candidate['title'])[0] == 'excluded':
                        # An explicit excluded title would be rejected after
                        # reading too; preserve its receipt without body/OCR/AI.
                        accept((candidate, {'excluded_by_title': 1}))
                        if len(candidates) >= _SAVE_BATCH_SIZE:
                            await flush()
                        continue
                    to_read.append(candidate)
                # Unseen current-scope posts first; known posts remain in this
                # same run for content-hash change detection/cache reuse.
                to_read.sort(key=lambda item: item['link'] in previous_links)
                live.update(site_documents_total=len(to_read), site_documents_processed=0)
                await emit_progress(phase='content', force=True)
                await read_bounded(session, to_read)
                await flush()
                if summary['state'] == 'shutdown':
                    break
                if outcome['state'] != 'finished':
                    await record_outcome('site_partial' if outcome['state'] == 'partial' else 'site_read_failed')
                else:
                    await record_outcome(None)
                    summary['sites'] += 1
        return summary
    except asyncio.CancelledError:
        # Preserve the completed reads in the current small batch, then stop.
        if (candidates or skipped_keys) and not save_failed:
            try:
                await flush()
            except Exception:
                pass  # Earlier durable batches remain available for a later run.
        await emit_progress(force=True)
        raise
    except Exception:
        return dict(summary, state='waiting',
                    stop_reason='site_save_failed' if save_failed else 'site_collection_failed')
    finally:
        close_error = None
        cancelled_during_close = False
        try:
            if engine is not None:
                close_task = asyncio.create_task(engine.close())
                while not close_task.done():
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        # Finish browser cleanup before releasing the OS lock.
                        cancelled_during_close = True
                        continue
                close_task.result()
        except Exception as error:
            close_error = error
        finally:
            _release_scrape_lock(handle)
        if cancelled_during_close:
            raise asyncio.CancelledError
        if close_error is not None:
            raise close_error

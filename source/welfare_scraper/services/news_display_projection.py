"""Read-only news presentation shared by the app and explicit exports.

Canonical source text remains in the saved analysis for grounding and retry.
Projection is in-memory unless the caller explicitly supplies its DB connection
to recover missing provenance from a historical import receipt. That recovery
only reads; it never writes records, changes selection authority or calls AI.
"""
import json
import sqlite3


def analysis_presentation(analysis, *, whole_source=False):
    from services.news_readable_content import presentation
    source = analysis if isinstance(analysis, dict) else {}
    return presentation(source, whole_source=whole_source)


def _item_source(item):
    ai = item.get('ai_data')
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except (ValueError, TypeError):
            ai = {}
    source = dict(ai) if isinstance(ai, dict) else {}
    if isinstance(item.get('support_detail'), str):
        source['support_detail'] = item['support_detail']
    return source


def is_policy_item(item):
    """Older welfare_items store their type inside ai_data, not a SQL column."""
    return item.get('item_type') == 'policy' or _item_source(item).get('item_type') == 'policy'


def _receipt_provenance(item, source, conn):
    """Prove whole-source origin in the caller's DB, not from the import flag."""
    if (conn is None or 'source_support_selection' in source or 'news_readable' in source
            or type(source.get('news_import_review')) is not bool
            or item.get('item_type') == 'policy' or source.get('item_type') == 'policy'
            or type(item.get('id')) is not int or item['id'] <= 0
            or not isinstance(item.get('link'), str) or not item['link'].strip()
            or not isinstance(source.get('support_detail'), str) or not source['support_detail']):
        return {}
    body = source['support_detail']
    # No schema initialization or generic job reader: those may revalidate and
    # rewrite a saved job. Bind the supplied item to the current saved row too.
    try:
        rows = conn.execute('''SELECT j.recipe,j.input_json,j.result_json
            FROM news_result_imports AS i
            JOIN news_analysis_jobs AS j ON j.job_id=i.job_id
            JOIN welfare_items AS w ON w.id=i.item_id AND w.link=i.link
            WHERE i.item_id=? AND i.link=? AND i.outcome='created'
              AND j.state='complete' AND w.support_detail=?
            ORDER BY i.created_at DESC LIMIT 8''', (item['id'], item['link'], body)).fetchall()
    except sqlite3.Error:
        # Older databases may not have these tables. Reading must not migrate
        # them or erase legitimate prose because provenance is unavailable.
        return {}
    from services import news_custom_analysis as custom, news_source_selection as selection
    for recipe, encoded_input, encoded_result in rows:
        try:
            options = custom.recipe_options(recipe)
            payload, result = json.loads(encoded_input), json.loads(encoded_result)
        except (TypeError, ValueError):
            continue
        if (not options['source_unit_rules'] or not isinstance(payload, dict)
                or not isinstance(result, dict) or payload.get('link') != item['link']
                or payload.get('content') != body or result.get('filter_reason') == 'invalid_saved_analysis'):
            continue
        analysis = result.get('analysis')
        if (not isinstance(analysis, dict) or analysis.get('support_detail') != body
                or selection.selection_issue(analysis, body, required=True,
                    extended=options['whole_document_rules'],
                    complete_document=options['complete_document_rules'])):
            continue
        # JSON-decoded metadata is detached from the caller and DB. Never copy
        # the old prose over a user's current edit, nor inherit approval flags.
        return {key: analysis[key] for key in ('source_support_selection', 'news_readable')
                if isinstance(analysis.get(key), dict)}
    return {}


def with_stored_provenance(item, conn):
    """Copy an item, enriching only proven metadata for old manual imports.

    Call while the caller owns a read connection, before handing data to an
    exporter. Ordinary legacy items and current imports require no query.
    """
    output = dict(item)
    source = _item_source(item)
    provenance = _receipt_provenance(item, source, conn)
    if provenance:
        output['ai_data'] = dict(source, **provenance)
    return output


def item_presentation(item, *, conn=None):
    """Use current saved prose; an import-review boolean is not provenance."""
    if is_policy_item(item):
        # News source extraction must not reinterpret a user's policy prose.
        body = _item_source(item).get('support_detail')
        return dict(support_detail=body if isinstance(body, str) else '',
                    presentation_kind='legacy', source_note='')
    if conn is not None:
        item = with_stored_provenance(item, conn)
    return analysis_presentation(_item_source(item))

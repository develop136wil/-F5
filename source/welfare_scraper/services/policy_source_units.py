"""Whole-context policy units; selection transport, never semantic approval."""
import hashlib

from services.policy_evidence import POLICY_PROSE_FIELDS
from services.policy_source_coverage import coverage_candidates

SELECTION_KEY = '_source_unit_selection'
MAX_SOURCE_UNITS = 128


def source_units(content):
    """Partition without cutting known headings, children or detached predicates.

    Unknown gaps remain complete units too. Units are not relevance decisions:
    no unit is inserted into an answer unless explicitly selected.
    """
    from services.policy_source_plan import source_lines, _context_units, PolicySourcePlanError
    lines = source_lines(content)
    merged = []
    for start, end in sorted(_context_units(content, lines)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    ranges, cursor = [], 1
    for start, end in merged:
        if cursor < start:
            ranges.append([cursor, start - 1])
        ranges.append([start, end])
        cursor = end + 1
    if cursor <= len(lines):
        ranges.append([cursor, len(lines)])
    candidates = coverage_candidates(content)
    catalog = []
    for start, end in ranges:
        kinds = sorted({row['kind'] for row in candidates if start <= row['line_number'] <= end})
        # Repeated table/header labels of the same kind are one context, not
        # isolated heading-only choices. Never bridge an unknown/neutral gap.
        if catalog and kinds and catalog[-1]['kinds'] == kinds and catalog[-1]['end_line'] + 1 == start:
            catalog[-1]['end_line'] = end
        else:
            catalog.append(dict(id=len(catalog) + 1, start_line=start, end_line=end,
                                kinds=kinds, required=bool(kinds)))
    if len(catalog) > MAX_SOURCE_UNITS:
        raise PolicySourcePlanError('source_structure_too_large')
    return catalog


def render_unit_selection(fields, content):
    """Rebuild only explicitly selected whole units, in original order.

    Adjacent units retain the original contiguous bytes. Disjoint units have a
    blank paragraph separator; their individual source locations remain in the
    verifiable selection metadata. No invented heading or prose is inserted.
    """
    from services.policy_source_plan import source_lines, PolicySourcePlanError
    lines = source_lines(content)
    units = source_units(content)
    if not isinstance(fields, dict) or set(fields) != set(POLICY_PROSE_FIELDS):
        raise PolicySourcePlanError('source_unit_invalid')
    selected, rendered = set(), {}
    for key in POLICY_PROSE_FIELDS:
        ids = fields[key]
        if (not isinstance(ids, list) or len(ids) > len(units)
                or any(type(value) is not int or not 1 <= value <= len(units) for value in ids)
                or ids != sorted(set(ids))):
            raise PolicySourcePlanError('source_unit_invalid')
        selected.update(ids)
        intervals = []
        for number in ids:
            unit = units[number - 1]
            start, end = unit['start_line'], unit['end_line']
            if intervals and start == intervals[-1][1] + 1:
                intervals[-1][1] = end
            else:
                intervals.append([start, end])
        pieces = [''.join(lines[start - 1:end]) for start, end in intervals]
        if any(not value.strip() for value in pieces):
            raise PolicySourcePlanError('source_unit_invalid')
        rendered[key] = '\n\n'.join(pieces)
    if any(unit['required'] and unit['id'] not in selected for unit in units):
        raise PolicySourcePlanError('source_sections_incomplete')
    return rendered


def hydrate_policy_source_units(result, content):
    from services.policy_source_plan import _STRUCTURED_KEYS, PolicySourcePlanError
    if set(result) - (_STRUCTURED_KEYS | {'_source_units'}):
        raise PolicySourcePlanError('source_unit_invalid')
    fields = result['_source_units']
    rendered = render_unit_selection(fields, content)
    hydrated = {key: value for key, value in result.items() if key in _STRUCTURED_KEYS}
    hydrated.update(rendered)
    hydrated['_source_evidence'] = {key: value for key, value in rendered.items() if value}
    hydrated[SELECTION_KEY] = dict(version=1, source_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                                  fields={key: list(fields[key]) for key in POLICY_PROSE_FIELDS})
    return hydrated


def verified_unit_fields(result, content):
    """Revalidate serialized provenance; never trust an internal-looking flag.

    Any edited field, quote, selection, source, ordering or missing required unit
    fails closed. This also applies after JSON cache/storage round trips.
    """
    from services.policy_source_plan import PolicySourcePlanError
    metadata = result.get(SELECTION_KEY)
    if (not isinstance(metadata, dict) or set(metadata) != {'version', 'source_sha256', 'fields'}
            or type(metadata.get('version')) is not int or metadata['version'] != 1
            or metadata.get('source_sha256') != hashlib.sha256(content.encode('utf-8')).hexdigest()):
        return None
    try:
        rendered = render_unit_selection(metadata['fields'], content)
    except PolicySourcePlanError:
        return None
    evidence = {key: value for key, value in rendered.items() if value}
    if result.get('_source_evidence') != evidence or any(result.get(key) != value for key, value in rendered.items()):
        return None
    return {key for key, value in rendered.items() if value}

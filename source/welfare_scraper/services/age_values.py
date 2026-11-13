"""Numeric safety contract, not an eligibility range or sentinel interpretation.

Preserve existing nonnegative month values (including unbounded-age markers).
Policy-specific age meaning belongs to the policy layer, not backup validation.
"""
import re

def month_value(value):
    if value is None or value == '':
        return None
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
        try:
            return int(value)
        except ValueError:  # Python bounds excessively long integer conversions.
            return None
    return None

def valid_month(value):
    return value is None or value == '' or month_value(value) is not None

def validate_restored_months(conn, tables):
    """Reject invalid restored values; never silently rewrite user data."""
    import json
    for table in ('welfare_items', 'notion_policies'):
        if table not in tables:
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        names = [k for k in ('start_age_months', 'end_age_months', 'ai_data', 'notion_fields', 'last_extracted_fields') if k in columns]
        if not names:
            continue
        for row in conn.execute('SELECT '+','.join('"'+k+'"' for k in names)+' FROM "'+table+'"'):
            for key, value in zip(names, row):
                if key in ('start_age_months', 'end_age_months'):
                    values = [value]
                else:
                    try:
                        parsed = json.loads(value or '{}')
                    except (ValueError, TypeError):
                        continue
                    values = [parsed[k] for k in ('start_age_months', 'end_age_months') if k in parsed] if isinstance(parsed, dict) else []
                if any(not valid_month(v) for v in values):
                    return '백업의 월령 값이 올바르지 않습니다. 0 이상의 정수 또는 빈값이어야 합니다.'
    return None

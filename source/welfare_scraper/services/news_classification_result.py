"""Basic classification shape shared by direct and durable news analysis."""
import json


def validate_result(result):
    if not isinstance(result, dict) or type(result.get('is_relevant')) is not bool:
        raise ValueError('Invalid AI relevance result')
    for key in ('category', 'target'):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError('Incomplete AI classification')
    for key in ('start_age_months', 'end_age_months'):
        value = result.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 1800):
            raise ValueError('Invalid AI age')
    start, end = result.get('start_age_months'), result.get('end_age_months')
    if start is not None and end is not None and start > end:
        raise ValueError('AI age range is reversed')
    json.dumps(result, allow_nan=False)

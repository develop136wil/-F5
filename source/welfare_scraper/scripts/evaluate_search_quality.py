"""실행 중인 로컬 복지F5의 대표 검색어 스모크 품질을 평가한다.

이 도구는 자동으로 서버를 시작하거나 API 키를 읽지 않는다. 사용자가 데이터 전송과
Gemini 한도 사용을 승인해 로컬 앱을 실행한 경우에만 명시한 base URL을 호출한다.
결과는 사람이 만든 정답 라벨 기반 정확도가 아니라 대상별 누락/순위 역전을 찾는 휴리스틱이다.
"""
import argparse
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request


SEARCHABLE_FIELDS = (
    'title', 'category', 'target', 'target_characteristics', 'life_cycle',
    'residence', 'eligibility_detail', 'support_detail',
)


def _text(item):
    parts = []
    for key in SEARCHABLE_FIELDS:
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value is not None:
            parts.append(str(value))
    return ' '.join(parts).casefold()


def evaluate_case(case, items, limit=5):
    required = [str(term).casefold() for term in case.get('required_any', []) if str(term).strip()]
    expected_categories = set(case.get('expected_categories') or [])
    inspected = []
    first_relevant_rank = None
    for rank, item in enumerate(items[:limit], 1):
        haystack = _text(item)
        matched_terms = [term for term in required if term in haystack]
        category_expected = not expected_categories or item.get('category') in expected_categories
        relevant = bool(matched_terms) and category_expected
        if relevant and first_relevant_rank is None:
            first_relevant_rank = rank
        inspected.append({
            'rank': rank,
            'title': item.get('title') or '',
            'category': item.get('category') or '',
            'match_sources': item.get('match_sources') or [],
            'matched_terms': matched_terms,
            'category_expected': category_expected,
            'heuristic_relevant': relevant,
        })
    return {
        'id': case.get('id'),
        'query': case.get('query'),
        'hit_at_5': first_relevant_rank is not None,
        'first_relevant_rank': first_relevant_rank,
        'reciprocal_rank': round(1 / first_relevant_rank, 4) if first_relevant_rank else 0.0,
        'results': inspected,
    }


def summarize(evaluations):
    total = len(evaluations)
    hits = sum(bool(row['hit_at_5']) for row in evaluations)
    return {
        'case_count': total,
        'hit_at_5_count': hits,
        'hit_at_5_rate': round(hits / total, 4) if total else 0.0,
        'mean_reciprocal_rank': round(
            sum(row['reciprocal_rank'] for row in evaluations) / total, 4
        ) if total else 0.0,
        'failed_case_ids': [row['id'] for row in evaluations if not row['hit_at_5']],
    }


def fetch_results(base_url, query, limit, token=''):
    params = urllib.parse.urlencode({'q': query, 'limit': limit})
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/search/semantic?{params}",
        headers={'X-Welfare-Session': token} if token else {},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode('utf-8'))


def main():
    parser = argparse.ArgumentParser()
    default_cases = Path(__file__).resolve().parents[1] / 'search_quality_cases.json'
    parser.add_argument('--base-url', default='http://127.0.0.1:8421')
    parser.add_argument('--cases', type=Path, default=default_cases)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')

    suite = json.loads(args.cases.read_text(encoding='utf-8'))
    token = os.getenv('WELFARE_DESKTOP_TOKEN', '')
    evaluations = []
    semantic_used_all = True
    notices = []
    for case in suite.get('cases', []):
        response = fetch_results(args.base_url, case['query'], args.limit, token)
        semantic_used_all = semantic_used_all and bool(response.get('semantic_used'))
        if response.get('notice'):
            notices.append({'id': case.get('id'), 'notice': response['notice']})
        evaluations.append(evaluate_case(case, response.get('items') or [], args.limit))

    report = {
        'suite_version': suite.get('version'),
        'measurement_kind': 'heuristic_search_smoke_not_accuracy',
        'semantic_used_for_all_cases': semantic_used_all,
        'summary': summarize(evaluations),
        'notices': notices,
        'cases': evaluations,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()

"""Offline diagnostic of cached public-title scores; never calibrates production."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.news_quality_evaluation import evaluate


def audit(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('state') != 'complete':
        raise ValueError('Incomplete source experiment')
    rows = report['rows']
    version = report['scoring_version']
    # Existing labels are provisional assistant reviews, not an independent test set.
    thresholds = sorted(set([0.8, 0.82, 0.84] + [row['score'] for row in rows]))
    metrics = [evaluate(rows, threshold=t, scoring_version=version) for t in thresholds]
    rankings = []
    for profile in sorted({row['profile_id'] for row in rows}):
        ordered = sorted((row for row in rows if row['profile_id'] == profile),
                         key=lambda row: row['score'], reverse=True)
        rankings.append({'profile': profile, 'ranked_candidates': [
            {key: row[key] for key in ('candidate_id', 'title', 'score', 'label')}
            for row in ordered]})
    return {'source': str(path), 'api_calls': 0, 'samples': len(rows),
            'unique_documents': len({r['candidate_id'] for r in rows}),
            'independent_labels': False, 'production_approved': False,
            'scope': 'cached title promotion, not full collection precision or recall',
            'perfect_separation_on_sample': any(m['false_positive'] == m['false_negative'] == 0 for m in metrics),
            'rankings': rankings, 'threshold_diagnostics': metrics}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = audit(args.source)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))

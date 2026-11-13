"""Offline audit of labelled similarity scores, not an eligibility decision.

No API calls, settings writes or automatic threshold selection. A promoted title
still requires content/target analysis. Unknown labels remain visible separately.
"""
import math


def evaluate(rows, *, threshold, scoring_version):
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError('Invalid threshold')
    if not isinstance(scoring_version, str) or not scoring_version.strip():
        raise ValueError('Scoring version required')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError('Provide 1..10000 labelled rows')
    counts = dict(true_positive=0, false_positive=0, true_negative=0,
                  false_negative=0, unknown=0, unknown_promoted=0, unscored=0,
                  relevant_unscored=0, promoted=0)
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid row')
        identity = (row.get('profile_id'), row.get('candidate_id'))
        if not all(isinstance(x, str) and x.strip() for x in identity) or identity in seen:
            raise ValueError('Missing or duplicate profile/candidate identity')
        seen.add(identity)
        if row.get('scoring_version') != scoring_version:
            raise ValueError('Mixed or missing scoring recipe')
        label, score = row.get('label'), row.get('score')
        if label not in ('relevant', 'irrelevant', 'unknown'):
            raise ValueError('Explicit reference label required')
        counts['unknown'] += label == 'unknown'
        if score is None:
            counts['unscored'] += 1
            counts['relevant_unscored'] += label == 'relevant'
            continue
        if type(score) not in (int, float) or not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError('Invalid score')
        promoted = score >= threshold
        counts['promoted'] += promoted
        if label == 'unknown':
            counts['unknown_promoted'] += promoted
        else:
            key = ('true_positive' if promoted else 'false_negative') if label == 'relevant' else (
                'false_positive' if promoted else 'true_negative')
            counts[key] += 1
    tp, fp, fn = (counts[k] for k in ('true_positive', 'false_positive', 'false_negative'))
    return dict(counts, samples=len(rows), threshold=threshold, scoring_version=scoring_version,
                precision=tp/(tp+fp) if tp+fp else None,
                recall_scored=tp/(tp+fn) if tp+fn else None,
                recall_including_unscored=tp/(tp+fn+counts['relevant_unscored'])
                if tp+fn+counts['relevant_unscored'] else None,
                score_coverage=(len(rows)-counts['unscored'])/len(rows),
                production_approved=False)

"""Read-only inventory of existing scoring evidence; no record contents or API calls."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def audit(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        tables = {r[1] for r in conn.execute('PRAGMA table_list')}
        counts = {}
        for name in ('welfare_items', 'news_profiles', 'news_run_snapshots',
                     'news_run_candidates', 'news_semantic_decisions', 'news_analysis_jobs'):
            counts[name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] if name in tables else None
        versions = []
        if 'news_semantic_decisions' in tables:
            versions = [dict(scoring_version=r[0], rows=r[1]) for r in conn.execute(
                'SELECT scoring_version,count(*) FROM news_semantic_decisions GROUP BY scoring_version')]
        return {'table_counts': counts, 'scoring_versions': versions,
                'reviewed_labels_verified': False, 'production_approved': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), ensure_ascii=False))

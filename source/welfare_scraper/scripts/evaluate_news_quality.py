"""Read reviewed score samples and print aggregate metrics. Never calls an API."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.news_quality_evaluation import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples', type=Path)
    parser.add_argument('--threshold', type=float, required=True)
    parser.add_argument('--scoring-version', required=True)
    args = parser.parse_args()
    try:
        if args.samples.stat().st_size > 10_000_000:
            raise ValueError('Input too large')
        rows = json.loads(args.samples.read_text(encoding='utf-8-sig'))
        result = evaluate(rows, threshold=args.threshold, scoring_version=args.scoring_version)
    except (OSError, ValueError):
        parser.exit(2, 'Invalid evaluation input; check schema, size, labels and scoring version.\n')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()

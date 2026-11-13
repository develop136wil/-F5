"""Bounded body analysis when no calibrated title-similarity gate is available.

This does not invent a similarity score or assert eligibility. Every selected
candidate still passes the normal grounded AI analysis and target rules.
"""
from dataclasses import dataclass
import json

from services import news_analysis_store as jobs
from services.news_custom_analysis import RECIPE
from services.news_profile_store import _atomic
from services.news_analysis_context import NewsAnalysisContext
from services.news_publication import analysis_payload


@dataclass(frozen=True)
class DirectAnalysis:
    scoring_version: str = 'direct-body-bounded-v1'


def queue_candidates(conn, run_id, model, limit):
    with _atomic(conn):
        context = NewsAnalysisContext.from_run(conn, run_id)
        if context.profile.general_mode != 'custom_v1':
            raise ValueError('Direct analysis requires custom target rules')
        rows = conn.execute('''SELECT candidate_key,payload_json FROM news_run_candidates
            WHERE run_id=? AND route='semantic_candidate' ORDER BY rowid LIMIT ?''',
            (run_id, limit)).fetchall()
        for key, encoded in rows:
            job_id = jobs.enqueue(conn, run_id, payload=analysis_payload(json.loads(encoded)), model=model, recipe=RECIPE)
            conn.execute("UPDATE news_run_candidates SET route='direct_analysis',job_id=? WHERE run_id=? AND candidate_key=?",
                         (job_id, run_id, key))
        return len(rows)

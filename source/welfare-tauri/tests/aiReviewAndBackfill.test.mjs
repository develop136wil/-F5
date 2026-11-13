import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { nextBackfillCursor, summarizeBackfillOutcome } from '../src/utils/embeddingBackfill.js';

test('length holds advance a bounded scan without pretending AI success', () => {
    const result = { embedded: 0, failed: 0, remaining: 300, held_for_input_size: 100,
        scanned: 100, next_cursor: 100, scan_complete: false };
    assert.equal(nextBackfillCursor(result, 0), 100);
    assert.equal(nextBackfillCursor({ ...result, next_cursor: null, scan_complete: true }, 100), null);
    const summary = summarizeBackfillOutcome(0, result);
    assert.equal(summary.status, 'partial');
    assert.match(summary.message, /단어 검색/);
    assert.doesNotMatch(summary.message, /이미 최신|생성 완료/);
});

test('failed requests, quota waits and nonadvancing cursors stop the loop', () => {
    const result = { embedded: 1, failed: 0, remaining: 10, next_cursor: 30 };
    assert.equal(nextBackfillCursor(result, 20), 30);
    for (const change of [{ failed: 1 }, { unavailable_reason: 'daily_quota_exhausted' },
        { next_cursor: 20 }, { next_cursor: 19 }, { next_cursor: null },
        { next_cursor: '30' }, { next_cursor: NaN }, { embedded: -1 }, { remaining: undefined }]) {
        assert.equal(nextBackfillCursor({ ...result, ...change }, 20), null);
    }
});

test('prior backend and successful completion stay compatible', () => {
    assert.equal(nextBackfillCursor({ embedded: 1, remaining: 2, failed: 0 }, 0), 0);
    assert.equal(nextBackfillCursor({ embedded: 0, remaining: 2, failed: 0 }, 0), null);
    assert.equal(summarizeBackfillOutcome(2, { remaining: 0 }).status, 'success');
});

test('both screens pass cursor and keep incomplete response review visible', () => {
    for (const page of ['Settings', 'Search']) {
        const text = fs.readFileSync(new URL(`../src/pages/${page}.jsx`, import.meta.url), 'utf8');
        assert.match(text, /backfillEmbeddings\(100, cursor\)/);
        assert.match(text, /nextBackfillCursor\(r, cursor\)/);
        assert.match(text, /guard < 1000/);
    }
    const labels = fs.readFileSync(new URL('../src/utils/newsWorkflowState.js', import.meta.url), 'utf8');
    assert.match(labels, /ai_response_review_required:/);
    assert.match(labels, /자동 재시도를 멈췄습니다/);
});

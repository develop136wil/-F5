import test from 'node:test';
import assert from 'node:assert/strict';
import { policyNeedsReview, policyReviewNotes, policyReviewNextStep, policyReviewIsMirrored } from '../src/utils/policyReviewOutcome.js';

test('정책 보류·분석 실패를 전체 성공과 구분한다', () => {
    assert.equal(policyNeedsReview({ running: false, review_count: 1 }), true);
    assert.equal(policyNeedsReview({ running: false, ai_failed: 1 }), true);
    for (const status of [null, {}, { running: true, review_count: 1 }, { review_count: 0 }, { review_count: '1' }]) {
        assert.equal(policyNeedsReview(status), false);
    }
});

test('보류 이유는 최대 20개와 제한된 텍스트만 표시한다', () => {
    const notes = policyReviewNotes({ review_notes: [null, {}, ...Array.from({ length: 30 }, () => ({
        title: '긴 제목'.repeat(100), note: '긴 설명'.repeat(200), raw: 'no raw source',
    }))] });
    assert.equal(notes.length, 20);
    assert.equal(notes[0].title.length, 120);
    assert.equal(notes[0].note.length, 500);
    assert.deepEqual(Object.keys(notes[0]), ['title', 'note', 'nextStep']);
    assert.deepEqual(policyReviewNotes({ review_notes: 'bad' }), []);
});

test('조건 보류는 반복 실행 대신 적용 기준 확인을 안내한다', () => {
    const [note] = policyReviewNotes({ review_notes: [{title: '정책', note: '확인 필요', code: 'source_criteria_review_required'}] });
    assert.match(note.nextStep, /같은 원문을 반복 점검해도 해결되지 않습니다/);
    assert.match(note.nextStep, /담당 기관/);
    assert.match(policyReviewNextStep('source_too_long'), /상세 출처/);
    assert.match(policyReviewNextStep('ai_input_review_required'), /민감정보/);
    assert.equal(policyReviewNextStep('<script>bad</script>'), policyReviewNextStep(undefined));
    assert.doesNotMatch(policyReviewNextStep(undefined), /<script>/);
});

test('통합 점검에 같은 사유가 복제된 경우에만 재진입 배너를 합친다', () => {
    const status = {running:false, review_count:1, ai_failed:0, review_notes:[{title:'정책', note:'소득 기준 확인 필요', code:'source_criteria_review_required'}]};
    assert.equal(policyReviewIsMirrored(status, structuredClone(status)), true);
    for (const other of [null, {...status,running:true}, {...status,review_count:2}, {...status,ai_failed:1}, {...status,review_notes:[]}, {...status,review_notes:[{title:'다른 정책',note:'다른 사유'}]}]) {
        assert.equal(policyReviewIsMirrored(status, other), false);
    }
    assert.equal(policyReviewIsMirrored({running:false,review_count:1}, {running:false,review_count:1}), false);
});

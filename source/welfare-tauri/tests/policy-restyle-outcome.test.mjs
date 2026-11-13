import test from 'node:test';
import assert from 'node:assert/strict';
import {restyleNeedsReview} from '../src/utils/policyRestyleOutcome.js';
test('일부 보류가 있으면 전체 성공으로 표시하지 않는다', () => {
    assert.equal(restyleNeedsReview({running:false, styled:3, rejected_fields:1}), true);
    assert.equal(restyleNeedsReview({running:false, styled:0, rejected_fields:2}), true);
});
test('진행중·구버전 미확인 상태를 실패로 추정하지 않는다', () => {
    for (const status of [null, {}, {running:true,rejected_fields:2}, {running:false,rejected_fields:0}, {rejected_fields:'unknown'}]) {
        assert.equal(restyleNeedsReview(status), false);
    }
});

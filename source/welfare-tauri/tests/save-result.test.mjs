import test from 'node:test';
import assert from 'node:assert/strict';
import { formatSaveResult } from '../src/utils/saveResult.js';

test('저장 결과가 없으면 기존 완료 문구를 사용할 수 있게 빈 문자열을 반환', () => {
    assert.equal(formatSaveResult(null), '');
});

test('실제 저장 및 중복 제외 건수를 함께 안내', () => {
    assert.equal(
        formatSaveResult({ saved: 3, skipped: 2 }),
        '3건 저장/갱신 · 2건 중복 제외',
    );
});

test('새 항목이 없어도 0건 저장을 명확하게 안내', () => {
    assert.equal(formatSaveResult({ saved: 0, skipped: 0 }), '0건 저장/갱신');
});

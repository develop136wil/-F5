import test from 'node:test';
import assert from 'node:assert/strict';
import { combineAgeParts, formatAgeMonths, splitAgeMonths } from '../src/utils/ageMonths.js';

test('개월 저장값을 만 나이와 추가 개월로 나눈다', () => {
    assert.deepEqual(splitAgeMonths(108), { years: 9, months: 0, total: 108 });
    assert.deepEqual(splitAgeMonths(113), { years: 9, months: 5, total: 113 });
});

test('만 나이와 추가 개월을 기존 설정 형식인 개월로 합친다', () => {
    assert.equal(combineAgeParts(9, 5), 113);
    assert.equal(combineAgeParts(20, 11), 240);
    assert.equal(combineAgeParts(-1, -1), 0);
});

test('사용자가 확인할 나이와 실제 저장 개월을 함께 설명한다', () => {
    assert.equal(formatAgeMonths(113), '만 9세 5개월 · 저장값 113개월');
});

test('선택 나이 범위의 0·11·12개월과 150세 경계를 정확히 왕복한다', () => {
    for (const total of [0,1,11,12,13,119,120,1799,1800]) {
        const parts=splitAgeMonths(total,1800);
        assert.equal(combineAgeParts(parts.years,parts.months,1800),total);
        assert.ok(parts.months>=0&&parts.months<=11);
    }
    assert.equal(combineAgeParts(149,11,1800),1799);
    assert.equal(combineAgeParts(150,11,1800),1800);
    assert.equal(combineAgeParts(0,12,1800),11);
    assert.equal(combineAgeParts(-1,-1,1800),0);
    assert.deepEqual(splitAgeMonths(1801,1800),{years:150,months:0,total:1800});
    assert.equal(formatAgeMonths(0),'만 0세 · 저장값 0개월');
    assert.equal(formatAgeMonths(11),'만 0세 11개월 · 저장값 11개월');
});

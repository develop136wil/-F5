import test from 'node:test';
import assert from 'node:assert/strict';
import { categoriesFromSummary } from '../src/utils/categories.js';

test('검색 분류는 현재 저장된 데이터에서 자동으로 만든다', () => {
    const result = categoriesFromSummary({ by_category: { '돌봄/양육': 2, '의료/재활': 1, null: 3 } });
    assert.deepEqual(result, ['돌봄/양육', '의료/재활']);
});

test('현재 선택값은 통계가 갱신돼도 필터에서 사라지지 않는다', () => {
    assert.deepEqual(categoriesFromSummary({ by_category: {} }, '기관 자체 분류'), ['기관 자체 분류']);
});

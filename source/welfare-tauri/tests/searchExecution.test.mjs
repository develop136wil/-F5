import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveSearchExecution } from '../src/utils/searchExecution.js';

test('통합 검색에서 분류만 선택해도 일반 필터 검색을 실행한다', () => {
    assert.deepEqual(
        resolveSearchExecution({ semanticMode: true, query: '', category: '돌봄/양육' }),
        { useSemanticSearch: false, enabled: true },
    );
});

test('두 글자 이상의 검색어가 있으면 통합 검색을 사용한다', () => {
    assert.deepEqual(
        resolveSearchExecution({ semanticMode: true, query: '아동', category: '' }),
        { useSemanticSearch: true, enabled: true },
    );
});

test('통합 검색 초기 화면은 불필요한 전체 조회를 하지 않는다', () => {
    assert.deepEqual(
        resolveSearchExecution({ semanticMode: true, query: '', category: '' }),
        { useSemanticSearch: false, enabled: false },
    );
});

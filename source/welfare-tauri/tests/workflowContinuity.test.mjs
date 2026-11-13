import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { queryCountStatus } from '../src/utils/queryCountStatus.js';
import { readSearchSession, updateSearchSession } from '../src/utils/searchSession.js';

test('건수 조회 전은 실제 0건과 구분한다', () => {
    assert.equal(queryCountStatus({}, undefined, '검토 대기'), '확인 중…');
    assert.equal(queryCountStatus({}, 0, '검토 대기'), '검토 대기 0건');
});
test('첫 조회 실패는 0건으로 표시하지 않는다', () => {
    assert.equal(queryCountStatus({isError:true}, undefined, '등록 후보'), '확인하지 못했습니다');
});
test('갱신 실패 시 이전 값임을 명시한다', () => {
    assert.equal(queryCountStatus({isError:true}, 12, '등록 후보'), '등록 후보 12건 · 이전 조회값 (갱신 실패)');
    assert.equal(queryCountStatus({isError:true}, 0, '등록 후보'), '등록 후보 0건 · 이전 조회값 (갱신 실패)');
});
test('재조회 중 이전 건수는 유지하며 갱신 상태를 알린다', () => {
    assert.equal(queryCountStatus({isFetching:true}, 3, '검토 대기'), '검토 대기 3건 · 갱신 중');
});
test('검색 화면 재진입에 사용할 조건과 페이지를 메모리에 보존한다', () => {
    updateSearchSession('inputQuery', '청년 주거');
    updateSearchSession('query', '청년 주거');
    updateSearchSession('category', '생활지원');
    updateSearchSession('page', 3);
    updateSearchSession('activeOnly', true);
    updateSearchSession('semanticMode', false);
    assert.deepEqual(readSearchSession(), {inputQuery:'청년 주거',query:'청년 주거',category:'생활지원',page:3,activeOnly:true,semanticMode:false});
    const copy = readSearchSession(); copy.query = '외부 변경';
    assert.equal(readSearchSession().query, '청년 주거');
});
test('페이지 함수형 변경을 보존하며 알 수 없는 필드는 무시한다', () => {
    updateSearchSession('page', 2);
    assert.equal(updateSearchSession('page', n => n + 1), 3);
    updateSearchSession('secret', 'not stored');
    assert.equal(readSearchSession().secret, undefined);
});
test('검색 조건은 디스크에 기록하지 않고 재진입 타이머가 페이지를 초기화하지 않는다', () => {
    const source = fs.readFileSync(new URL('../src/pages/Search.jsx', import.meta.url), 'utf8');
    const store = fs.readFileSync(new URL('../src/utils/searchSession.js', import.meta.url), 'utf8');
    assert.doesNotMatch(store, /(?:localStorage|sessionStorage)\.(?:setItem|getItem)/);
    assert.match(source, /if \(next !== readSearchSession\(\)\.query\)/);
    assert.match(source, /useSearchSessionField\('page'\)/);
});

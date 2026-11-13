import test from 'node:test';
import assert from 'node:assert/strict';
import {
    applyVerifiedPolicyTitles,
    createPolicyDbDraft,
    parsePolicyDbs,
    policyDbPayload,
} from '../src/constants/policyDbs.js';

test('정책 보관함은 고정 종류 없이 유효한 연결만 정규화함', () => {
    const rows = parsePolicyDbs([
        { key: 'custom', name: '직접 만든 표', category: '청소년', data_source_id: '  abc  ' },
        { key: 'empty', name: '빈 행', data_source_id: ' ' },
    ]);
    assert.deepEqual(rows, [{ key: 'custom', name: '직접 만든 표', category: '청소년', data_source_id: 'abc' }]);
});

test('연결 확인에서 받은 실제 Notion 제목을 로컬 표시명에 반영함', () => {
    const rows = [{ key: 'custom', name: '정책표 1', category: '', data_source_id: 'abc' }];
    assert.equal(applyVerifiedPolicyTitles(rows, [{ key: 'custom', title: '도봉구 청소년 지원' }])[0].name, '도봉구 청소년 지원');
});

test('빈 입력은 연결 해제용 빈 배열이 되고 새 행 키는 서로 다름', () => {
    assert.deepEqual(policyDbPayload([createPolicyDbDraft(0)]), []);
    assert.notEqual(createPolicyDbDraft(0).key, createPolicyDbDraft(1).key);
});

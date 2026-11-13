import test from 'node:test';
import assert from 'node:assert/strict';
import { applyWithSourceConfirmation } from '../src/utils/policyReviewConfirmation.js';

test('원문 미확인 취소는 반영 요청을 보내지 않는다', async () => {
    let writes = 0;
    const result = await applyWithSourceConfirmation({field: '지원 내용', requires_individual_review: true},
        '주차 정책', message => { assert.match(message, /제외 조건/); return false; }, () => { writes++; });
    assert.equal(result, false);
    assert.equal(writes, 0);
});
test('확인 후 해당 반영만 한 번 실행한다', async () => {
    let writes = 0;
    assert.equal(await applyWithSourceConfirmation({field: '지원 내용', requires_individual_review: true},
        '주차 정책', () => true, () => { writes++; }), true);
    assert.equal(writes, 1);
});
test('기존 태그 반영에는 불필요한 추가 확인을 요구하지 않는다', async () => {
    let writes = 0;
    await applyWithSourceConfirmation({field: '분류'}, '주차 정책', () => { throw Error('unexpected'); }, () => { writes++; });
    assert.equal(writes, 1);
});
test('실패를 성공으로 바꾸거나 자동 재시도하지 않는다', async () => {
    let attempts = 0;
    await assert.rejects(applyWithSourceConfirmation({requires_individual_review: true}, '정책', () => true,
        async () => { attempts++; throw Error('offline'); }), /offline/);
    assert.equal(attempts, 1);
});

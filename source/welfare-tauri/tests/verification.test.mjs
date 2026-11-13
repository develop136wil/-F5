import test from 'node:test';
import assert from 'node:assert/strict';
import { currentVerification } from '../src/utils/verification.js';

test('설정 변경 후 이전 성공 결과는 완료로 표시하지 않음', () => {
    assert.equal(currentVerification('new', { revision: 'old', ok: true }), undefined);
});
test('현재 설정의 실패 결과를 이전 성공으로 덮지 않음', () => {
    const failed = { revision: 'new', ok: false };
    assert.equal(currentVerification('new', failed, { revision: 'old', ok: true }), failed);
});
test('설정 조회 전이나 토큰 저장만으로는 완료되지 않음', () => {
    assert.equal(currentVerification(undefined, { ok: true }), undefined);
    assert.equal(currentVerification('new', { notion_token_configured: true }), undefined);
});

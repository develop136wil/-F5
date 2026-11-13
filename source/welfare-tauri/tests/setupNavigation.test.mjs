import test from 'node:test';
import assert from 'node:assert/strict';
import { featureSetupSteps, firstIncompleteSetupStep, nextSetupStep, isConnectionFormVisible } from '../src/utils/setupNavigation.js';

test('정책과 갤러리는 필요한 연결만 순서대로 준비한다', () => {
    assert.deepEqual(featureSetupSteps('policy'), ['google', 'signup', 'notion', 'template', 'policy']);
    assert.deepEqual(featureSetupSteps('news'), ['google', 'signup', 'notion', 'template', 'news']);
    assert.equal(firstIncompleteSetupStep('policy', {}), 'google');
    assert.equal(firstIncompleteSetupStep('news', { google: true, signup: true, notion: true }), 'template');
});

test('이미 준비된 연결을 건너뛰고 저장한 단계 다음으로 이동한다', () => {
    assert.equal(nextSetupStep('policy', 'google', { signup: true, notion: true }), 'template');
    assert.equal(nextSetupStep('policy', 'google', {}), 'signup');
    assert.equal(nextSetupStep('news', 'notion', {}), 'template');
    assert.equal(nextSetupStep('news', 'news', {}), null);
    assert.equal(firstIncompleteSetupStep('policy', { google: true, signup: true, notion: true, template: true, policy: true }), null);
});

test('연결된 보관함도 명시적 변경 요청이면 입력칸이 표시된다', () => {
    assert.equal(isConnectionFormVisible(true, 'news', 'news'), true);
    assert.equal(isConnectionFormVisible(true, null, 'news'), false);
    assert.equal(isConnectionFormVisible(true, 'policy', 'news'), false);
    assert.equal(isConnectionFormVisible(false, null, 'news'), true);
});

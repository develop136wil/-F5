import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
test('도우미는 공통 단계 탐색을 사용하고 완료 상태와 현재 선택을 구분한다', () => {
    const guide = read('../src/components/onboarding/GuidedSetup.jsx');
    const steps = read('../src/components/onboarding/SetupSteps.jsx');
    assert.equal((guide.match(/<SetupSteps/g) || []).length, 1);
    assert.match(steps, /aria-current=\{activeKey === item.key \? 'step'/);
    assert.match(steps, /done\[item.key\] \? <Check/);
    assert.match(steps, /disabled=\{disabled\}/);
    assert.match(steps, /현재 단계/);
    assert.doesNotMatch(guide, /shortLabel \|\| step.label|h-1.5 rounded-full mb-2/);
});
test('저장 조건 영역은 카드 요약과 생성을 제공하고 직접 실행은 숨긴다', () => {
    const panel = read('../src/components/NewsProfilesPanel.jsx');
    const settings = read('../src/pages/Settings.jsx');
    assert.match(panel, /!editing && !creating/);
    assert.match(panel, /!embedded && !p.protected && p.execution_enabled/);
    assert.match(panel, /<NewsConditionCardSummary profile=\{p\}/);
    assert.match(read('../src/components/NewsConditionCardSummary.jsx'),/profile.characteristics/);
    assert.match(panel, /분석 기록 보기/);
    assert.doesNotMatch(settings, /CUSTOM_AI_PROMPT|loadKeywords|loadExcludedKeywords|이전 수집 설정/);
});

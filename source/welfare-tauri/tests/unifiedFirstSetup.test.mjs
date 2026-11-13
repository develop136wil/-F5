import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {featureSetupItems} from '../src/utils/setupNavigation.js';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');
test('첫 진입은 기능 선택 이전에 통합 도우미를 연다',()=>{
    const home=read('../src/pages/Home.jsx');
    assert.match(home,/step=setup/);
    assert.match(home,/설정 시작하기/);
    assert.deepEqual(featureSetupItems('policy').map(x=>x.label),['Gemini 설정','Notion 가입','Notion API','템플릿 복제','주소 등록']);
});
test('템플릿 본문에서만 선택하며 주소 확인 뒤 내부 카드로 돌아간다',()=>{
    const guide=read('../src/components/onboarding/GuidedSetup.jsx');
    assert.match(guide,/unified&&!templateChoice \? <SetupTemplateChooser/);
    assert.match(guide,/if \(unified\) \{\s*setTemplateChoice\(null\);\s*setSelectedSection\(\{mode:'policy',key:'template'\}\);\s*return;/);
    assert.match(guide,/!\(unified&&!templateChoice&&activeSection==='template'\)/);
    const cards=read('../src/components/onboarding/SetupTemplateChooser.jsx');
    assert.match(cards,/done\[item.key\]/);
    assert.doesNotMatch(cards,/saveConfig|startNewsWorkflow/);
});
test('Notion 건너뛰기는 수집 조건 안내로 이동하고 완료나 저장을 만들지 않는다',()=>{
    const guide=read('../src/components/onboarding/GuidedSetup.jsx');
    const skip=guide.split('\n').find(line=>line.includes('Notion은 나중에 설정 · 수집 준비하기'));
    assert.match(skip,/navigate\('\/settings\?tab=guide&step=target'\)/);
    assert.match(skip,/disabled=\{saving\|\|settingsDrafts.isWriting\(\)\}/);
    assert.doesNotMatch(skip,/saveConfig|setAcknowledged|startNewsWorkflow/);
});

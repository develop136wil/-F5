import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {featureSetupSteps,firstIncompleteSetupStep} from '../src/utils/setupNavigation.js';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');

test('두 기능은 공통 세 단계를 유지하고 각자의 복제·주소 등록으로 이어진다',()=>{
    for(const mode of ['policy','news']){
        assert.deepEqual(featureSetupSteps(mode),['google','signup','notion','template',mode]);
        assert.equal(firstIncompleteSetupStep(mode,{google:true,signup:true,notion:true}),'template');
    }
    const cards=read('../src/components/onboarding/SetupFeatureCards.jsx');
    assert.equal((cards.match(/key:'(?:policy|news)',title:/g)||[]).length,2);
    assert.doesNotMatch(cards,/title:'Notion 보관함 연결'/);
    assert.match(cards,/설정 완료 · 다시 보기/);
    assert.match(cards,/currentVerification\(config\?\.revision,item.check.data\)/);
    assert.match(cards,/!item.check.isError&&!item.check.isFetching/);
    assert.match(cards,/!settingsDrafts.dirty\(item.field\)/);
});

test('최종 주소 연결 성공과 현재 입력 상태를 확인한 뒤 카드로 돌아간다',()=>{
    const source=read('../src/components/onboarding/GuidedSetup.jsx');
    const advance=source.slice(source.indexOf('const advanceAfterSave'),source.indexOf('const current = CORE_STEPS'));
    assert.match(advance,/if \(!isCurrentView\(\)\) return/);
    assert.ok(advance.indexOf('settingsDrafts.dirty')<advance.indexOf("navigate('/settings?tab=guide'"));
    assert.match(advance,/featureMode && key === featureMode/);
    assert.match(source,/if \(result.ok\)[\s\S]*?advanceAfterSave\('policy'\)/);
    assert.match(source,/if \(!result.ok\)[\s\S]*?else[\s\S]*?advanceAfterSave\('news'\)/);
});

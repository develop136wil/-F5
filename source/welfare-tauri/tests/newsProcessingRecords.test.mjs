import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('기본 소식은 ready만 조회하고 보조 기록은 펼칠 때만 별도로 조회한다',()=>{
    const main=readFileSync(new URL('../src/pages/NotionUpload.jsx',import.meta.url),'utf8');
    const records=readFileSync(new URL('../src/components/NewsProcessingRecords.jsx',import.meta.url),'utf8');
    assert.match(main,/getNewsFeed\(\{profileId:scope.id,view:'ready'/);
    assert.doesNotMatch(main,/changeView|setView/);
    assert.match(main,/counts=\{isError \? undefined : data\?\.counts\}/);
    assert.match(records,/Object.keys\(views\).every\(view => counts\?\.\[view\] === 0\)/);
    assert.match(main,/<NewsProcessingRecords key=\{scope.id\}/);
    assert.match(records,/useState\(false\)/);
    assert.match(records,/open&&<Records/);
    assert.match(records,/processing:'분석 대기·실패',review:'추가 확인',records:'제외·종료'/);
    assert.doesNotMatch(records,/prepareNewsFeedAction|importNewsResult|uploadToNotion|exportNewsGallery|type="checkbox"/);
    assert.match(records,/onDetail\(item\)/);
    assert.match(records,/수집 진행으로 이동/);
});

test('연결 작업 없는 이전 실패는 재개를 약속하지 않고 조건·AI 확인으로 안내한다',()=>{
    const records=readFileSync(new URL('../src/components/NewsProcessingRecords.jsx',import.meta.url),'utf8');
    assert.match(records,/view==='processing'&&items.some\(item=>item.workflow_id\)/);
    assert.match(records,/item.recovery_action==='new_collection'&&!item.workflow_id/);
    assert.match(records,/수집 조건 확인·저장/);
    assert.match(records,/settings\?tab=keywords/);
    assert.match(records,/authentication_required/);
    assert.match(records,/settings\?tab=guide&step=google/);
    assert.doesNotMatch(records,/startNewsWorkflow|startScrape|resumeNewsWorkflow|retryAnalysis/);
});

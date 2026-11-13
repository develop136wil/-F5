import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {canClearHistory,splitHistory,latestTerminalWorkflow,historyConfirmation,historyDate} from '../src/utils/newsHistory.js';

test('마지막 완료·종료·재개 불가 실패 하나만 요약하고 새 작업과 이전 조건은 가리지 않는다',()=>{
    const done={workflow_id:'done',state:'complete',revision:1};
    const old={...done,workflow_id:'old'};
    for(const state of ['complete','cancelled','failed']) {
        const latest={...done,state,can_resume:false};
        assert.equal(latestTerminalWorkflow([latest,old],1),latest);
    }
    assert.equal(latestTerminalWorkflow([],1),null);
    assert.equal(latestTerminalWorkflow([done],2),null);
    for(const state of ['ready','running','waiting'])assert.equal(latestTerminalWorkflow([{...done,state},old],1),null);
    assert.equal(latestTerminalWorkflow([done,{...old,state:'running'}],1),null);
    assert.equal(latestTerminalWorkflow([{...done,state:'failed',can_resume:true},old],1),null);
});
test('완료 카드는 기본 진행 영역을 차지하지 않고 현재 대기·실행은 유지',()=>{
    const done={workflow_id:'done',state:'complete',revision:1,can_clear_history:true};
    const pending={workflow_id:'pending',state:'waiting',revision:1,can_resume:true};
    const older={...pending,workflow_id:'old',revision:0};
    const result=splitHistory([done,pending,older],1);
    assert.ok(result.foreground.includes(pending));assert.ok(result.history.includes(done));
    assert.equal(canClearHistory(done),true);
    assert.equal(canClearHistory({...done,state:'waiting',can_resume:false,resume_block_reason:'news_source_scope_changed'}),true);
    for(const item of [pending,{...done,active:true},{...done,can_clear_history:false},{...done,state:'failed',can_resume:true}])assert.equal(canClearHistory(item),false);
});
test('분리한 실행 상세와 마크다운 컴포넌트의 JSX가 컴파일된다',()=>{
    const {transformSync}=createRequire(import.meta.url)('esbuild');
    for(const name of ['NewsProfileRunPanel','ReadableMarkdown','NewsFeedDetailDialog','ItemDetailModal']) {
        const source=readFileSync(new URL(`../src/components/${name}.jsx`,import.meta.url),'utf8');
        assert.ok(transformSync(source,{loader:'jsx',format:'esm'}).code.length>0);
    }
});
test('정리 확인은 소식 보존과 실제 삭제 기준을 구분',()=>{
    assert.match(historyConfirmation,/목록에서 정리/);assert.match(historyConfirmation,/삭제하지 않습니다/);
    assert.match(historyConfirmation,/7일·30일/);assert.equal(historyDate(null),'날짜 정보 없음');
    assert.notEqual(historyDate('2026-09-17 12:30:00'),'날짜 정보 없음');
});
test('완료 이력은 기본 접힘이고 카드 본문은 상세 창에서만 표시',()=>{
    const panel=readFileSync(new URL('../src/components/NewsProfileRunPanel.jsx',import.meta.url),'utf8');
    assert.match(panel,/useState\(false\)/);assert.match(panel,/historyOpen&&/);assert.match(panel,/window.confirm\(historyConfirmation\)/);
    const page=readFileSync(new URL('../src/pages/NotionUpload.jsx',import.meta.url),'utf8');
    const card=page.slice(page.indexOf('<article'),page.indexOf('</article>'));
    assert.doesNotMatch(card,/support_preview|support_detail|it\.target/);
    assert.match(card,/it\.review_required/);assert.match(card,/attachment_review_required/);
});

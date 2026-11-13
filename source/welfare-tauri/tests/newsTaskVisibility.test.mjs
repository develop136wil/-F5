import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readHiddenTasks,writeHiddenTasks,hideTaskConfirmation} from '../src/utils/newsTaskVisibility.js';
import {blocksNewWorkflow,canResumeWorkflow} from '../src/utils/newsWorkflowState.js';
test('숨김은 프로필별로 보존하고 작업 차단·재개 상태는 변경하지 않는다',()=>{
    const values=new Map(),storage={getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)};
    const running={workflow_id:'run',state:'running',active:true,revision:1},waiting={workflow_id:'wait',state:'waiting',can_resume:true,revision:1};
    assert.equal(writeHiddenTasks('a',['run','wait','run'],storage),true);
    assert.deepEqual(readHiddenTasks('a',storage),['run','wait']);assert.deepEqual(readHiddenTasks('b',storage),[]);
    assert.equal(blocksNewWorkflow(running,1),true);assert.equal(blocksNewWorkflow(waiting,1),true);assert.equal(canResumeWorkflow(waiting),true);
    writeHiddenTasks('a',['wait'],storage);assert.deepEqual(readHiddenTasks('a',storage),['wait']);
});
test('오염된 저장값·저장 실패는 안전하게 처리하고 목록은 100개로 제한',()=>{
    for(const value of ['null','{}','bad'])assert.deepEqual(readHiddenTasks('a',{getItem:()=>value}),[]);
    const storage={getItem:()=>{throw Error('blocked');},setItem:()=>{throw Error('full');}};
    assert.deepEqual(readHiddenTasks('a',storage),[]);assert.equal(writeHiddenTasks('a',['1'],storage),false);
    let saved;writeHiddenTasks('a',[null,'',...Array.from({length:105},(_,i)=>String(i))],{setItem:(_k,v)=>saved=v});
    assert.equal(JSON.parse(saved).length,100);assert.equal(JSON.parse(saved)[0],'5');
    assert.match(hideTaskConfirmation,/계속 실행/);assert.match(hideTaskConfirmation,/자동 재개되지 않습니다/);
});

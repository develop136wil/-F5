import test from 'node:test';
import assert from 'node:assert/strict';
import {readNewsWorkflow,readNewsWorkflowHistory,readNewsProfiles,readNewsWorkflowOptions,readNewsDefaultCondition} from '../src/utils/newsResponse.js';
const task={workflow_id:'run',state:'running',active:true};
test('불완전 성공 응답은 빈 목록이나 완료로 처리하지 않는다',()=>{
    for(const value of [null,undefined,{},[],false,'ok',{items:null},{items:{}}]){
        assert.throws(()=>readNewsWorkflowHistory(value));assert.throws(()=>readNewsProfiles(value));
    }
    for(const value of [{},null,{state:'running'},{workflow_id:'run'}, {...task,state:'unknown'},{...task,active:'false'}])assert.throws(()=>readNewsWorkflow(value));
    assert.throws(()=>readNewsWorkflow(task,'different-run'));
    assert.throws(()=>readNewsWorkflowHistory({items:[task,{}]}));
    assert.throws(()=>readNewsProfiles({items:[{profile_id:''}]}));
    for(const value of [{},null,{enabled:'true'},{enabled:true},{enabled:true,selection:{revision:1}}])assert.throws(()=>readNewsWorkflowOptions(value));
    for(const value of [{},null,{profile:{}},{profile:undefined}])assert.throws(()=>readNewsDefaultCondition(value));
});
test('정상 빈 상태·준비 미완료·구형 선택적 active 필드는 호환한다',()=>{
    assert.equal(readNewsWorkflow(task),task);
    assert.deepEqual(readNewsWorkflow({workflow_id:'old',state:'complete'}),{workflow_id:'old',state:'complete'});
    assert.deepEqual(readNewsWorkflowHistory({items:[]}),{items:[]});
    assert.deepEqual(readNewsProfiles({items:[{profile_id:'default-news'}]}),{items:[{profile_id:'default-news'}]});
    assert.deepEqual(readNewsWorkflowOptions({enabled:false}),{enabled:false});
    const options={enabled:true,selection:{revision:1,life_cycles:[],characteristics:[]}};
    assert.equal(readNewsWorkflowOptions(options),options);
    assert.deepEqual(readNewsDefaultCondition({profile:null}),{profile:null});
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {isConfirmedOsTaskRemoval,normalizeOsTaskState} from '../src/utils/osTaskState.js';

test('OS 작업 해제는 명시적인 성공과 미등록이 함께 확인되어야 한다',()=>{
    assert.equal(isConfirmedOsTaskRemoval({status:'success',registered:false}),true);
    for(const result of [undefined,{}, {registered:false},{status:'success',registered:true},{status:'error',registered:false},{status:'success',registered:null}])assert.equal(isConfirmedOsTaskRemoval(result),false);
});

test('OS 조회 실패와 상태 누락은 미등록으로 바꾸지 않는다',()=>{
    for(const result of [{},{supported:true,registered:null,error:'조회 실패'},{supported:true,registered:false,error:'권한 오류'},{supported:true,registered:'false'}]){
        const state=normalizeOsTaskState(result);assert.equal(state.registered,null);assert.ok(state.error);
    }
    assert.deepEqual(normalizeOsTaskState({supported:true,registered:false}),{supported:true,registered:false,error:''});
    assert.deepEqual(normalizeOsTaskState({supported:true,registered:true}),{supported:true,registered:true,error:''});
});

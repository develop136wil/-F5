import test from 'node:test';
import assert from 'node:assert/strict';
import {createProfileResults} from '../src/utils/profileResults.js';
test('늦은 목록 응답은 최신 프로필 버전과 이름을 되돌리지 않는다', () => {
    const store=createProfileResults();
    store.observe({profile_id:'a',revision:3,name:'새 이름'});
    store.observe({profile_id:'a',revision:2,name:'이전 이름'});
    assert.equal(store.latest({profile_id:'a',revision:1}).revision,3);
    assert.equal(store.latest({profile_id:'a',revision:1}).name,'새 이름');
    assert.equal(store.latest({profile_id:'a',revision:4}).revision,4);
});
test('저장 실패 결과는 구독 화면이 없어도 남고 성공 시 교체된다', () => {
    const store=createProfileResults();
    store.report('a',{ok:false,message:'실패'});
    let calls=0; const stop=store.subscribe(()=>calls++);
    assert.equal(store.outcome('a').ok,false);
    store.report('a',{ok:true,message:'완료'});
    assert.equal(calls,1); assert.equal(store.outcome('a').ok,true);
    assert.equal(store.outcome('b'),undefined); stop();
});

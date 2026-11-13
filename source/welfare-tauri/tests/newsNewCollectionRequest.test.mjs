import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/api/welfare.js',import.meta.url),'utf8');
const method=source.match(/^    startNewsWorkflow:.*$/m)[0];

test('새 회차 원문 수집은 해당 요청의 실제 true만 전달하고 기존 호출은 유지한다',async()=>{
    const calls=[];
    const api=new Function('client',`return {${method}}`)({post:async(url,body)=>{calls.push({url,body});return {data:{ok:true}};}});
    for(const choice of [undefined,false,1,'true',true]) {
        await api.startNewsWorkflow('profile /1',3,{newCollection:choice});
        assert.deepEqual(calls.at(-1),{url:'/news/profiles/profile%20%2F1/workflows',body:{expected_revision:3,headless:true,...(choice===true?{new_collection:true}:{})}});
    }
    await api.startNewsWorkflow('p',2);
    assert.deepEqual(calls.at(-1).body,{expected_revision:2,headless:true});
});

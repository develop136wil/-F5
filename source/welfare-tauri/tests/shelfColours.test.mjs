import {test} from 'node:test';
import assert from 'node:assert/strict';
import {shelfColours} from '../src/utils/shelfColours.js';
test('8개까지 색상을 구분하고 목록 순서가 바뀌어도 유지한다',()=>{
 const keys=['medical','education','care','family','living','extra','custom','other'];
 const before=shelfColours(keys);
 assert.equal(new Set(before.values()).size,8);
 assert.deepEqual(shelfColours([...keys].reverse()),before);
 assert.deepEqual(shelfColours([...keys,keys[0]]),before);
});
test('빈 목록과 많은 사용자 보관함도 처리한다',()=>{
 assert.equal(shelfColours([]).size,0);
 assert.equal(shelfColours(Array.from({length:20},(_,i)=>String(i))).size,20);
});

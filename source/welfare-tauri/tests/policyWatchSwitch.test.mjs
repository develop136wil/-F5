import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
test('웹페이지 스위치는 상태와 중복 요청 방지를 제공한다',()=>{
 const s=readFileSync(new URL('../src/pages/PolicyWatch.jsx',import.meta.url),'utf8');
 assert.match(s,/role="switch" aria-checked=\{Boolean\(it.enabled\)\}/);
 assert.match(s,/disabled=\{toggling.has\(it.id\)\}/);
 assert.match(s,/togglePending.current.has\(item.id\)/);
 assert.match(s,/await reload\(\)/);
 assert.doesNotMatch(s,/ShieldAlert|purple-/);
});

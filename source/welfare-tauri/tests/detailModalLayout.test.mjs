import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('상세창은 긴 제목에서도 본문만 줄여 원문 버튼 공간을 확보한다',()=>{
 const source=readFileSync(new URL('../src/components/ItemDetailModal.jsx',import.meta.url),'utf8');
 assert.match(source,/relative flex flex-col/);
 assert.match(source,/max-h-\[85dvh\]/);
 assert.match(source,/min-h-0 flex-1 overflow-y-auto/);
 assert.match(source,/shrink-0 p-4 border-t/);
 assert.doesNotMatch(source,/calc\(85vh-140px\)/);
 assert.match(source,/원문 보기/);
 assert.match(source,/tone = 'default'/);
 assert.match(source,/bg-teal-700/);
 assert.match(source,/hover:bg-teal-800/);
 assert.match(source,/text-red-500/);
 assert.match(source,/border-amber-200/);
});

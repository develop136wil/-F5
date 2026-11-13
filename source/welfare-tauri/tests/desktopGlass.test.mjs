import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('로딩 카드는 초점을 유지하되 테두리 강조 없이 유리 효과를 사용한다',()=>{
 const source=readFileSync(new URL('../src/components/DesktopLifecycle.jsx',import.meta.url),'utf8');
 const card=source.match(/<section ref=\{connectionRef\}[\s\S]*?className="([^"]+)"/);
 assert.ok(card);
 assert.match(card[0],/tabIndex=\{-1\} role="dialog" aria-modal="true"/);
 assert.match(card[1],/outline-none/);
 assert.match(card[1],/backdrop-blur-xl/);
 assert.ok(card[1].includes('bg-white/[0.85]'));
 assert.match(source,/connectionRef\.current\?\.focus/);
 assert.match(source,/target\.focus\(\{ preventScroll: true \}\)/);
 assert.doesNotMatch(source,/<button[^>]*outline-none/);
});

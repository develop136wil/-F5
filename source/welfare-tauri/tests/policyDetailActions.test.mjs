import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('세부 작업은 문장 다듬기를 포함한 다섯 카드로 묶는다',()=>{
 const source=readFileSync(new URL('../src/pages/PolicyManage.jsx',import.meta.url),'utf8');
 const detail=source.split('{advancedOpen && (')[1].split('{/* 한 번에 점검 진행 */}')[0];
 assert.match(detail,/sm:grid-cols-2/);
 assert.ok(detail.indexOf('정책 작성 스타일 설정') < detail.indexOf('onClick={handleRestyle}'));
 assert.match(detail,/정책 확인·불러오기/);
 assert.doesNotMatch(detail,/<RefreshCw|<RefreshCcwDot|<ScanSearch/);
 for(const handler of ['handleSync','handleCheck','handleAudit','handleRecompute','handleRestyle'])
   assert.ok(detail.includes(`onClick={${handler}}`));
 assert.equal((detail.match(/>AI 사용 없음<\/span>/g)||[]).length,1);
 assert.ok(source.indexOf('onClick={handleRestyle}')<source.indexOf('정책 내용 변경 감지'));
 assert.equal((source.match(/onClick=\{handleRestyle\}/g)||[]).length,1);
 assert.match(detail,/disabled=\{!allChanges.length \|\| !dbKeys.length/);
 assert.match(source,/wrapperClassName="w-full sm:w-1\/2"/);
 assert.match(source,/min-h-14 w-full justify-center/);
 assert.match(source,/체크한 항목에만 적용되는 작업이 아닙니다/);
 assert.match(source,/restylePolicyChanges\(dbKeys\)/);
 assert.match(source,/aria-controls="policy-detail-actions"/);
});

test('정책 불러오기는 조건부 검색용 AI 사용을 알리고 저장된 제안 재검토는 AI를 쓰지 않는다',()=>{
 const source=readFileSync(new URL('../src/pages/PolicyManage.jsx',import.meta.url),'utf8');
 const sync=source.split('onClick={handleSync}')[1].split('onClick={handleCheck}')[0];
 assert.match(sync,/>필요 시 AI 사용<\/span>/);
 assert.doesNotMatch(sync,/AI 사용 없음/);
 assert.match(sync,/새 정책·제목이 바뀐 정책·검색용 데이터가 없는 정책/);
 assert.match(sync,/제목을 Gemini로 보내 검색용 데이터를 만들 수 있습니다/);
 assert.match(sync,/정책 내용 비교는 별도 작업입니다/);
 const recompute=source.split('onClick={handleRecompute}')[1].split('문장 다듬기')[0];
 assert.match(recompute,/>AI 사용 없음<\/span>/);
 assert.match(source,/onClick=\{handleFullCheck\} aria-describedby="policy-ai-usage"/);
 assert.ok(source.indexOf('id="policy-ai-usage"') < source.indexOf('{advancedOpen && ('));
 assert.match(source,/최초·변경 비교와 필요한 문장 정리·검색 데이터 생성에 AI를 사용합니다/);
 assert.match(source,/Notion에는 자동으로 반영하지 않습니다/);
});

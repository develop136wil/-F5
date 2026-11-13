import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('연결 안내는 한 줄 배치와 초점 툴팁을 지원한다',()=>{
 const settings=readFileSync(new URL('../src/pages/Settings.jsx',import.meta.url),'utf8');
 const paid=readFileSync(new URL('../src/components/AiUsageStatus.jsx',import.meta.url),'utf8');
 assert.match(settings,/flex flex-wrap items-start gap-x-5 gap-y-2[^>]*><PaidApiHelp compact/);
 assert.match(paid,/<Tooltip content=\{note\}>/);
 assert.doesNotMatch(paid,/title="Google 공식 안내/);
 assert.match(paid,/유료 프로젝트는 사용량에 따라 비용이 발생할 수 있습니다. 앱에서 결제 설정을 변경하지 않습니다/);
 assert.doesNotMatch(paid,/자동 결제 전환 없음/);
 for(const label of ['연결 확인·변경','새 소식 연결','정책 최신화 연결'])
   assert.ok(settings.includes(`<span>Notion</span><span className="whitespace-nowrap">${label}</span>`));
 assert.doesNotMatch(settings,/handleDisconnectNews|새 소식 보관함 연결 해제/);
 for(const route of ['policy&edit=notion','news&edit=news','policy&edit=policy'])
   assert.ok(settings.includes(`/settings?tab=guide&step=${route}`));
});

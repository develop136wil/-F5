import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/components/onboarding/GuidedSetup.jsx',import.meta.url),'utf8');
test('연결 변경 버튼은 대상을 명시하고 도우미 안에서 편집한다',()=>{
 for(const name of ['Gemini 연결·변경','Notion 연결 토큰 변경','새 소식 보관함 연결·변경','정책표 연결·변경']) assert.ok(source.includes(`editLabel="${name}"`));
 assert.doesNotMatch(source,/이 단계에서 변경|연결 관리에서 변경|처음 설정으로 돌아가기/);
 assert.match(source,/설정 안내 목록으로/);
 assert.match(source,/description="연결이 끝나도[^\n]+onEdit=\{\(\) => setEditing\('google'\)\}/);
});
test('첫 설치의 새 소식 분기에는 중복 건너뛰기 링크를 표시하지 않는다',()=>{
 assert.match(source,/!unified && featureMode === 'news' && <Link to="\/settings\?tab=guide&step=target"/);
});

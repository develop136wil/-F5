import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {MANUAL_TOPICS,MANUAL_TEXT,manualTopic,NEWS_MANUAL_NOTE} from '../src/constants/setupManual.js';
const read = p => readFileSync(new URL(p,import.meta.url),'utf8');

test('튜토리얼은 PDF의 가입·토큰·페이지 권한 절차를 실제 버튼명으로 안내한다',()=>{
 const guide=read('../src/components/onboarding/SetupWalkthrough.jsx');
 assert.match(guide,/linkLabel: 'Google AI Studio 열기'/);
 assert.doesNotMatch(guide,/Google AI Studio API 키 목록 열기/);
 assert.match(guide,/매뉴얼은 무료 요금제를 선택하는 예시/);
 assert.match(guide,/토큰 저장만으로는 정책표나 보관함에 접근할 수 없습니다/);
 for(const label of ['보관함','정책표']){
  assert.ok(guide.includes(`복제한 ${label}에 연결 권한 추가하기`));
  assert.ok(guide.includes(`템플릿 원본이 아닌 내 ${label}에서 진행하세요.`));
 }
 assert.equal((guide.match(/권한을 확인한 뒤 페이지에 추가를 눌러 마칩니다/g)||[]).length,2);
 assert.match(guide,/저장하고 보관함 연결 확인/);
 assert.match(guide,/저장하고 정책표 연결 확인/);
 assert.match(NEWS_MANUAL_NOTE,/튜토리얼의 ‘새 소식 보관함 템플릿 열기’ 버튼/);
 assert.doesNotMatch(NEWS_MANUAL_NOTE,/아래의/);
 assert.match(read('../src/components/onboarding/GuidedSetup.jsx'),/새 소식 수집과 소식 모음 파일 저장은 Notion 없이도/);
});
test('매뉴얼 단계가 실제 PDF 페이지에 연결되고 모든 페이지에 큰 글씨 안내가 있다',()=>{
 assert.deepEqual(MANUAL_TOPICS.google.pages,[11,12,13]);
 assert.deepEqual(MANUAL_TOPICS.signup.pages,[15,16,17]);
 assert.deepEqual(MANUAL_TOPICS.notion.pages,[18,19]);
 assert.equal(manualTopic('notion','signup'),'signup');
 assert.equal(manualTopic('news','register'),'news-register');
 for(const section of Object.values(MANUAL_TOPICS))for(const page of section.pages){
  assert.ok(MANUAL_TEXT[page]?.length);
  assert.ok(existsSync(new URL(`../public/manual/page-${page}.webp`,import.meta.url)));
 }
 assert.match(NEWS_MANUAL_NOTE,/정책 최신화 템플릿 기준/);
 assert.match(NEWS_MANUAL_NOTE,/새 소식 전용 템플릿/);
});
test('안내 창은 앱 수집·연결 생명주기를 시작하지 않으며 보조 창 닫기를 구분한다',()=>{
 const viewer=read('../src/manual.jsx');
 assert.doesNotMatch(viewer,/DesktopLifecycle|ScrapeProvider|welfareApi/);
 assert.match(viewer,/NEWS_MANUAL_NOTE/);
 assert.match(read('../src-tauri/src/lib.rs'),/window.label\(\) != "main"/);
 assert.match(read('../src-tauri/src/manual.rs'),/create_new\(true\)/);
 assert.doesNotMatch(viewer,/세로로 이어 보기|안내 글씨|<select/);
 assert.match(viewer,/이전 페이지/);
 assert.match(viewer,/다음 페이지/);
 assert.doesNotMatch(read('../src/components/onboarding/SetupDialog.jsx'),/<iframe|SetupManualContext/);
});

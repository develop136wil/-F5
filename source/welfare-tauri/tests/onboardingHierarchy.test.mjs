import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read = path => readFileSync(new URL(path,import.meta.url),'utf8');

test('주 단계와 도움말 현재 행동은 제목 수준과 크기가 구분된다',()=>{
    const guide=read('../src/components/onboarding/GuidedSetup.jsx');
    const help=read('../src/components/onboarding/SetupWalkthrough.jsx');
    assert.match(guide,/<h2 className="text-xl font-semibold/);
    assert.match(guide,/<h3 data-setup-heading className="text-base font-semibold/);
    assert.match(help,/<h4 data-setup-action[^>]*className=\{isTableGuide\?'ui-subtitle/);
    assert.match(help,/data-setup-help-label className=\{isTableGuide\?'block text-sm font-semibold/);
    assert.match(help,/'mt-1.5 text-sm font-semibold/,'기존 AI 도움말 위계 유지');
    assert.doesNotMatch(help,/border-y|border-t|border-b/);
    assert.doesNotMatch(help,/text-white|bg-indigo-600/,'설명 넘기기는 실제 저장과 같은 주 버튼을 쓰지 않음');
});

test('외부 행동은 현재 도움말 안에서 한 번 제공하고 템플릿 주소를 유지한다',()=>{
    const guide=read('../src/components/onboarding/GuidedSetup.jsx');
    const help=read('../src/components/onboarding/SetupWalkthrough.jsx');
    const action=read('../src/components/onboarding/ExternalActionLink.jsx');
    assert.doesNotMatch(guide,/<ExternalActionLink/);
    assert.match(guide,/phase="template" templateHref=\{featureMode === 'news' \? NOTION_NEWS_TEMPLATE_URL : NOTION_TEMPLATE_URL\}/);
    assert.match(guide,/items=\{unified \? featureSetupItems\('policy'\)/);
    assert.match(help,/externalAction && <ExternalActionLink/);
    assert.match(help,/index === 0 && templateHref/);
    assert.match(action,/target="_blank"/);
    assert.match(action,/rel="noopener noreferrer"/);
    assert.match(action,/aria-label=\{`\$\{title\} — 새 창으로 열림`\}/);
    assert.doesNotMatch(action,/>공식 페이지<|>새 창으로 열립니다<|>다음:/,'동일 외부 링크 주변에 반복 설명 행을 만들지 않음');
});

test('PDF 안내를 제공하고 키 입력칸 이동과 앞뒤 설명을 보존한다',()=>{
    const help=read('../src/components/onboarding/SetupWalkthrough.jsx');
    assert.match(help,/\/guide-images\/google-api-list.png/);
    assert.match(help,/\/guide-images\/notion-create.png/);
    assert.match(help,/\/guide-images\/notion-workspace.png/);
    assert.match(help,/<SetupManualLink/);
    assert.doesNotMatch(help,/화면 예시 보기|진행이 막혔나요\?/);
    assert.match(help,/document.getElementById\(inputId\)/);
    assert.match(help,/input.focus\(\{ preventScroll: true \}\)/);
    assert.match(help,/이전 설명/);
    assert.match(help,/다음 설명 보기/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { buildNotionSetupGuideSteps, buildOnboardingProgress } from '../src/utils/tutorialStatus.js';
import { NOTION_POLICY_TEMPLATE_FIELDS } from '../src/constants/notionTemplateFields.js';

const guidedSetupUrl = new URL('../src/components/onboarding/GuidedSetup.jsx', import.meta.url);
const setupWalkthroughUrl = new URL('../src/components/onboarding/SetupWalkthrough.jsx', import.meta.url);
const settingsUrl = new URL('../src/pages/Settings.jsx', import.meta.url);
const policyManageUrl = new URL('../src/pages/PolicyManage.jsx', import.meta.url);
const policyWatchUrl = new URL('../src/pages/PolicyWatch.jsx', import.meta.url);

test('설정의 주요 제목과 고급 하위 제목은 다른 수준과 크기를 유지한다', async () => {
    const settings = await readFile(settingsUrl, 'utf8');
    assert.match(settings, /<h1 className="ui-page-title/);
    const styles = await readFile(new URL('../src/index.css', import.meta.url), 'utf8');
    assert.match(styles, /\.ui-page-title \{ @apply text-\[1\.75rem\] font-bold/);
    assert.match(styles, /\.ui-section-title \{ @apply text-xl font-semibold/);
    const headings = [...settings.matchAll(/<(h[23])\b([^>]*)>([\s\S]*?)<\/\1>/g)];
    assert.ok(headings.length > 10);
    for (const [, tag, attrs] of headings) {
        assert.match(attrs, tag === 'h2' ? /ui-section-title|text-xl font-semibold/ : /ui-subtitle|text-base font-semibold/);
    }
    for (const label of ['정책 작성 스타일 가이드', '실시간 로그', '시스템 로그 파일', '백업 다운로드', '데이터 복원']) {
        assert.equal(headings.find(([, , , content]) => content.includes(label))?.[1], 'h3', label);
    }
});

test('공개 템플릿 주소는 사용 가능 안내만 바꾸고 복제 완료로 판정하지 않음', () => {
    const withoutTemplate = buildNotionSetupGuideSteps({ templateAvailable: false });
    const withTemplate = buildNotionSetupGuideSteps({ templateAvailable: true });

    assert.equal(withoutTemplate[0].done, false);
    assert.equal(withoutTemplate[0].note, '공개 준비 중');
    assert.equal(withTemplate[0].done, false);
    assert.equal(withTemplate[0].note, '템플릿 사용 가능');
});

test('Notion 단계는 저장·보관함 등록·실제 스키마 검증 결과를 각각 반영함', () => {
    const steps = buildNotionSetupGuideSteps({
        tokenConfigured: true,
        configuredPolicyDbCount: 2,
        notionReady: true,
    });

    assert.deepEqual(steps.slice(1).map(step => step.done), [true, true, true]);
    assert.match(steps[2].line, /2개 등록/);
});

test('잘못된 보관함 개수는 완료로 오판하지 않음', () => {
    for (const configuredPolicyDbCount of [undefined, null, '', -1, '잘못된 값']) {
        const steps = buildNotionSetupGuideSteps({ configuredPolicyDbCount });
        assert.equal(steps[2].done, false);
        assert.match(steps[2].line, /0개 등록/);
    }
});

test('기본 준비·정책 준비·새 소식 Notion 준비 진행률을 서로 섞지 않음', () => {
    const progress = buildOnboardingProgress({
        google: true,
        target: true,
        sites: false,
        notion: true,
        policy: false,
    });

    assert.deepEqual(progress, {
        collectionCompleted: 2,
        collectionTotal: 3,
        collectionReady: false,
        policyCompleted: 2,
        policyTotal: 3,
        policyReady: false,
        newsCompleted: 1,
        newsTotal: 2,
        newsReady: false,
    });
});

test('각 준비 과정은 자신의 필수 단계가 모두 끝나야 완료됨', () => {
    const progress = buildOnboardingProgress({
        google: true,
        target: true,
        sites: true,
        notion: true,
        policy: true,
    });

    assert.equal(progress.collectionReady, true);
    assert.equal(progress.policyReady, true);
    assert.equal(progress.newsReady, false);
    assert.equal(progress.collectionCompleted, progress.collectionTotal);
    assert.equal(progress.policyCompleted, progress.policyTotal);
});

test('공개 템플릿 대체 안내는 코드와 같은 15개 필수 항목을 중복 없이 제공함', () => {
    assert.equal(NOTION_POLICY_TEMPLATE_FIELDS.length, 15);
    assert.equal(new Set(NOTION_POLICY_TEMPLATE_FIELDS.map(field => field.name)).size, 15);
    assert.deepEqual(
        NOTION_POLICY_TEMPLATE_FIELDS.slice(-3).map(field => [field.name, field.type]),
        [['관련 홈페이지 1', 'URL'], ['관련 홈페이지 2', 'URL'], ['관련 홈페이지 3', 'URL']],
    );
});

test('초기 설정 진행표에는 꼭 필요한 세 단계만 표시함', async () => {
    const source = await readFile(guidedSetupUrl, 'utf8');
    assert.match(source, /const CORE_STEPS = \[/);
    assert.doesNotMatch(source, /shortLabel: 'Notion'/);
    assert.match(source, /<SetupSteps/);
    const steps = await readFile(new URL('../src/components/onboarding/SetupSteps.jsx', import.meta.url), 'utf8');
    assert.match(steps, /text-sm leading-snug/);
    assert.doesNotMatch(steps, /truncate/);
});

test('지역과 나이 설정은 적용 범위와 단일 편집 위치를 분명히 안내함', async () => {
    const [guide, settings] = await Promise.all([
        readFile(guidedSetupUrl, 'utf8'),
        readFile(settingsUrl, 'utf8'),
    ]);
    const editor = await readFile(new URL('../src/components/DefaultNewsConditionEditor.jsx', import.meta.url), 'utf8');
    assert.match(guide, /<DefaultNewsConditionEditor/);
    assert.match(settings, /<DefaultNewsConditionEditor/);
    assert.match(editor, /새 소식과 자동 수집에 사용할 조건/);
    assert.match(editor, /정책 최신화에는 적용되지 않습니다/);
    assert.match(editor, /submitted\.revision/);
});

test('새 소식 수집 범위를 정책 최신화의 대상처럼 표시하지 않음', async () => {
    const [policyManage, policyWatch] = await Promise.all([
        readFile(policyManageUrl, 'utf8'),
        readFile(policyWatchUrl, 'utf8'),
    ]);

    assert.doesNotMatch(policyManage, /TARGET_REGION|TARGET_AGE_MAX_MONTHS|대상:\s*\{targetLabel\}/);
    assert.doesNotMatch(policyWatch, /TARGET_REGION|TARGET_AGE_MAX_MONTHS|대상:\s*\{targetLabel\}/);
});

test('수집 설정은 필수 항목과 선택 고급 항목을 구분함', async () => {
    const settings = await readFile(settingsUrl, 'utf8');

    assert.doesNotMatch(settings, /이전 수집 설정|CUSTOM_AI_PROMPT|welfareApi\.getKeywords/);
    assert.match(settings, /저장한 다른 조건/);
    assert.doesNotMatch(settings, /대상별 수집 조건 만들기/);
    const tutorial = await readFile(new URL('../src/pages/Tutorial.jsx', import.meta.url), 'utf8');
    assert.doesNotMatch(tutorial, /대상별 수집 조건 만들기/);
    assert.match(tutorial, /저장한 다른 조건/);
    assert.match(settings, /<DefaultNewsConditionEditor\s*\/>[\s\S]*?저장한 다른 조건[\s\S]*?<NewsProfilesPanel embedded\s*\/>[\s\S]*?<\/section>/);
    assert.doesNotMatch(settings, /다른 대상 수집 시험 준비/);
    assert.match(settings, /스타일 편집<\/span>/);
    assert.match(settings, /<details id="policy-writing-style"/);
    assert.doesNotMatch(settings, /스타일 편집<ChevronDown/);
});

test('Notion과 시스템 설정도 필수 기능과 고급 기능을 구분함', async () => {
    const settings = await readFile(settingsUrl, 'utf8');

    assert.match(settings, /새 소식 연결/);
    assert.doesNotMatch(settings, /새 소식 Notion 저장 방법/);
    assert.match(settings, /Notion 연결 토큰/);
    assert.match(settings, /정책 작성 스타일/);
    assert.match(settings, /사이트 수집 상태/);
    assert.match(settings, /고급 시스템 관리/);
    const backupHeading = settings.indexOf('<Database className="w-5 h-5 text-indigo-500" /> 데이터 백업 및 복원');
    const advancedHeading = settings.indexOf('>고급 시스템 관리</span>');
    assert.ok(backupHeading >= 0 && advancedHeading >= 0 && backupHeading < advancedHeading);
    assert.match(settings, /사이트별 상세 상태[\s\S]*?<\/details>\s*\{\/\* 가장 중요한 안전 기능/);
});

test('숨긴 시스템 진단 도구는 펼칠 때만 상태와 로그를 읽음', async () => {
    const settings = await readFile(settingsUrl, 'utf8');

    assert.match(settings, /activeTab !== 'system' \|\| !systemHealthOpen/);
    assert.match(settings, /activeTab !== 'system' \|\| !systemAdvancedOpen/);
    assert.doesNotMatch(settings, /activeTab === 'logs' \|\| activeTab === 'system'/);
});

test('Gemini와 Notion 연결은 가상 화면 없이 단계별 실제 메뉴 이름으로 따라갈 수 있음', async () => {
    const [guide, walkthrough] = await Promise.all([
        readFile(guidedSetupUrl, 'utf8'),
        readFile(setupWalkthroughUrl, 'utf8'),
    ]);

    assert.match(guide, /<SetupWalkthrough kind="google" completed=\{done\.google\}/);
    assert.match(guide, /<SetupStageInstructions kind="notion"/);
    assert.match(guide, /<SetupStageInstructions kind="news" phase="register"/);
    assert.match(guide, /<SetupStageInstructions kind="policy" phase="register"/);
    assert.doesNotMatch(guide, /<SetupWalkthrough[^>]*defaultOpen/);
    assert.match(guide, /\{done\.google && <CompletedConnection/);
    assert.match(guide, /\{done\.notion && <CompletedConnection/);
    assert.match(guide, /\{done\.policy && <><CompletedConnection/);
    assert.doesNotMatch(guide, /done\.(?:google|notion|policy) \? <CompletedConnection/);
    for (const heading of [
        'Google AI Studio에서 API 키 확인하기', '새 키 만들기', '표시된 키 전체 복사', '복지F5에 붙여넣고 확인',
        '내 Notion에 정책표 만들기', '내 Notion에 보관함 만들기', '복제한 보관함에 연결 권한 추가하기', '보관함 주소 복사',
        '내부 연결 만들기', '권한 확인 후 토큰 복사', '복제한 정책표에 연결 권한 추가하기', '주소 붙여넣고 연결 확인',
    ]) assert.match(walkthrough, new RegExp(heading));
    assert.doesNotMatch(walkthrough, /MockScreen/);
    assert.match(walkthrough, /화면에서 그대로 찾을 이름/);
    assert.match(walkthrough, /필요할 때 다시 확인하세요/);
    assert.match(walkthrough, /다음 설명 보기/);
    assert.match(walkthrough, /export const SetupStageInstructions/);
    assert.doesNotMatch(walkthrough, /진행이 막혔나요|화면 예시 보기/);
    assert.match(walkthrough, /<SetupManualLink/);
    assert.match(walkthrough, /이전/);
    assert.match(walkthrough, /다음/);
});

test('연결 설정은 한 탭으로 통합하고 예전 주소도 호환한다', async () => {
    const settings = await readFile(settingsUrl, 'utf8');
    assert.match(settings, /const validTabs = \['connections', 'keywords', 'automation', 'system', 'guide'\]/);
    assert.match(settings, /\['general', 'notion'\]\.includes\(requestedTab\) \? 'connections'/);
    assert.match(settings, /tab === 'connections' && '연결 관리'/);
    assert.doesNotMatch(settings, /tab === 'general' && '기본 설정'/);
    assert.doesNotMatch(settings, /tab === 'notion' && 'Notion 연동'/);
    assert.match(settings, /<AnimatePresence mode='sync'>/);
    assert.match(settings, /새 소식 연결/);
    assert.match(settings, /settings\?tab=guide&step=news/);
    assert.match(settings, /settings\?tab=guide&step=policy/);
    assert.match(settings, /정책 최신화 연결/);
    assert.doesNotMatch(settings, /id="google-gemini-api-key"/);
    assert.doesNotMatch(settings, /aria-label="Notion 연결 토큰" type="password"/);
    assert.match(settings, /flex flex-col items-stretch gap-4[^"\n]*sm:flex-row sm:items-center/);
});

test('도움말 딥링크는 새 소식과 정책 준비를 별도 흐름으로 연다', async () => {
    const guide = await readFile(guidedSetupUrl, 'utf8');
    assert.match(guide, /const FEATURE_GUIDES = \{/);
    assert.match(guide, /Object\.hasOwn\(FEATURE_GUIDES, requestedGuideStep\)/);
    assert.match(guide, /featureMode === 'policy'/);
    assert.match(guide, /settings\?tab=guide&step=news/);
    assert.match(guide, /settings\?tab=guide&step=policy/);
    assert.match(guide, /Notion 없이 수집 준비하기/);
    assert.match(guide, /AI와 복지F5 연결/);
});

test('초기 설정 진행 현황은 매우 좁은 PC 창에서 제목 아래로 안전하게 내려간다', async () => {
    const guide = await readFile(guidedSetupUrl, 'utf8');
    assert.match(guide, /flex flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between/);
    assert.doesNotMatch(guide, /필요한 항목만 표시 중/);
    assert.match(guide, /함께 쓰는 공통 연결입니다/);
    const steps = await readFile(new URL('../src/components/onboarding/SetupSteps.jsx', import.meta.url), 'utf8');
    assert.match(steps, /aria-current/);
    assert.match(steps, /activeKey === item.key \? 'border-blue-200 bg-blue-50/);
    assert.match(steps, /aria-label=\{`\$\{index \+ 1\}/,'상태는 접근성 이름으로 제공');
});

test('도움말은 초기 설정 흐름만 두고 별도 상세 연결 도움말을 반복하지 않는다', async () => {
    const tutorial = await readFile(new URL('../src/pages/Tutorial.jsx', import.meta.url), 'utf8');
    assert.match(tutorial, /<SetupDialog \/>/);
    assert.doesNotMatch(tutorial, /<GuidedSetup/);
    assert.doesNotMatch(tutorial, /Gemini·Notion 상세 발급 및 연결 도움말/);
    assert.doesNotMatch(tutorial, /<SetupWalkthrough/);
});

test('현재 Google·Notion 용어를 사용하고 오래된 키 모양을 완료 조건으로 오인하지 않음', async () => {
    const [guide, walkthrough, settings] = await Promise.all([
        readFile(guidedSetupUrl, 'utf8'),
        readFile(setupWalkthroughUrl, 'utf8'),
        readFile(settingsUrl, 'utf8'),
    ]);
    const combined = `${guide}\n${walkthrough}\n${settings}`;

    assert.match(combined, /Installation access token/);
    assert.match(walkthrough, /Read content/);
    assert.match(walkthrough, /Update content/);
    assert.match(walkthrough, /Insert content/);
    assert.match(guide, /Google AI Studio에서 복사한 키 전체/);
    assert.doesNotMatch(combined, /AIza로 시작|보통 AIza|Internal Integration Secret/);
});

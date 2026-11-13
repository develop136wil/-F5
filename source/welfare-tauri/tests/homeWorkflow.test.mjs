import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { queryKeys } from '../src/api/queryKeys.js';

const read = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8');
const home = read('../src/pages/Home.jsx');
const workspace = read('../src/components/HomeWorkspace.jsx');
const news = read('../src/pages/NotionUpload.jsx');
const newsRecords = read('../src/components/NewsProcessingRecords.jsx');
const panel = read('../src/components/NewsCollectionPanel.jsx');
const context = read('../src/context/ScrapeContext.jsx');

test('홈 업무를 색상과 문구로 구분하고 실제 이동 링크만 제공한다', () => {
    assert.match(workspace, /정책 변경 확인하기/);
    assert.match(workspace, /새 소식 탐색하기/);
    assert.doesNotMatch(workspace, /업무 화면으로 이동합니다|기존 정보 관리|새로운 정보 발견|진행 순서|Notion·HTML 저장/);
    assert.match(workspace, /수집 사이트 등록·관리/);
    assert.match(workspace, /from-teal-50/);
    assert.match(workspace, /from-blue-50/);
    assert.match(workspace, /pointer-events-none absolute/);
    assert.match(workspace, /<Link to=\{to\}/);
    assert.doesNotMatch(workspace, /onClick=|welfareApi/);
});

test('홈은 두 업무의 입구이고 수집을 자동 실행하지 않는다', () => {
    assert.doesNotMatch(home + workspace, /정보 수집 시작|가장 먼저 할 일|초기 설정 안내를 숨겨|startScrape/);
    assert.match(workspace, /aria-label="주요 업무" className="grid grid-cols-\[repeat\(auto-fit,minmax\(min\(100%,18rem\),1fr\)\)\]/);
    assert.match(workspace, /to="\/policy"/);
    assert.match(workspace, /to="\/notion"/);
    assert.match(workspace, /policyStatus \?\? `검토 대기 \$\{policyCount\}건`/);
    assert.match(workspace, /등록 후보 \$\{newsCount\}건/);
    assert.match(workspace, /aria-label="바로가기" className="grid grid-cols-\[repeat\(auto-fit,minmax\(min\(100%,10rem\),1fr\)\)\]/);
});

test('주 업무 카드만 여유 있게 확장하고 새 필수 설명을 더하지 않는다', () => {
    assert.match(workspace, /min-h-\[304px\]/);
    assert.match(workspace, /xl:min-h-\[352px\]/);
    assert.match(workspace, /min-h-\[72px\]/);
    assert.doesNotMatch(workspace, /h-screen|h-\[100vh\]/);
    assert.match(home, /수집할 지역·카테고리·나이 확인/);
});

test('설정 안내를 숨겨도 작은 설정 도우미 링크로 다시 접근할 수 있다', () => {
    assert.match(home, /showOnboarding && !setupCardCollapsed/);
    assert.doesNotMatch(home, /showOnboarding && setupCardCollapsed/);
    assert.match(home, /<Link to="\/settings\?tab=guide" className="inline-flex shrink-0/);
});

test('새소식에는 수집·중지·설정 경로가 있고 저장 동작과 구분한다', () => {
    assert.match(news, /<NewsCollectionPanel actionBusy=\{busy\}/);
    assert.match(panel, /새 소식 찾기/);
    assert.match(panel, /onClick=\{stopScrape\}/);
    assert.match(panel, /role="progressbar"/);
    assert.match(panel, /\/settings\?tab=keywords/);
    assert.match(panel, /ai\.data\?\.busy/);
    assert.match(news, /const actionsDisabled = busy \|\| isScraping \|\| isStarting/);
    assert.match(news, /<NewsProcessingRecords/);
    assert.match(newsRecords, /<Link to="\/search"[^>]*>기존 저장 자료 검색<\/Link>/);
    assert.doesNotMatch(news, /<NewsProfileResults|새 소식 후보로 가져오기/);
    assert.match(news, /isError \? \(/);
});

test('기본 수집의 새 위치를 설정 도우미·안내·검색에서 일관되게 설명한다', () => {
    const guidance = ['../src/components/onboarding/GuidedSetup.jsx', '../src/pages/Tutorial.jsx',
        '../src/pages/Settings.jsx', '../src/pages/Search.jsx', '../src/components/NewsProfilesPanel.jsx'].map(read).join('\n');
    assert.doesNotMatch(guidance, /홈에서.*수집|홈의.*수집/);
    assert.match(guidance, /새 소식 찾으러 가기/);
    assert.match(guidance, /소식 모음 파일 저장은 Notion 없이 가능/);
});

test('Notion 미연결 안내는 HTML 사용을 막지 않고 해당 준비 과정으로 연결한다', () => {
    assert.doesNotMatch(news, /<AlertTriangle className="mt-1 w-4 h-4 shrink-0" \/>/);
    assert.match(news, /<p className="text-sm text-gray-500">소식 모음 파일 저장은 Notion 없이도 사용할 수 있습니다\./);
    assert.match(news, /<Link to="\/settings\?tab=guide&step=news"/);
});

// Execute the real provider command functions without network/React rendering.
function commands(api, running = false) {
    const states = [], invalidated = [];
    let stateIndex = 0;
    const env = {
        useState: value => { const id = stateIndex++; return [id === 0 ? running : value, next => states.push([id, next])]; },
        useRef: value => ({current: value}), useCallback: fn => fn, useEffect() {},
        useQueryClient: () => ({invalidateQueries: args => invalidated.push(args.queryKey)}),
        welfareApi: api, queryKeys, getErrorMessage: e => e.message,
        toast: {info() {}, warning() {}, error() {}},
    };
    const start = context.indexOf('export const ScrapeProvider = ({ children }) => {') + 'export const ScrapeProvider = ({ children }) => {'.length;
    const body = context.slice(start, context.indexOf('    return (\n        <ScrapeContext.Provider', start));
    const result = new Function(...Object.keys(env), `${body}\nreturn {startScrape,stopScrape,invalidateData};`)(...Object.values(env));
    return {...result, states, invalidated};
}

test('시작 요청 응답이 느려도 연속 클릭은 실제 시작 1회만 보낸다', async () => {
    let calls = 0, finish;
    const c = commands({startScrape: () => { calls++; return new Promise(resolve => { finish = resolve; }); }});
    const first = c.startScrape(); await c.startScrape();
    assert.equal(calls, 1);
    finish(); await first;
    assert.ok(c.states.some(([id, value]) => id === 1 && value === true));
    assert.ok(c.states.some(([id, value]) => id === 1 && value === false));
});

test('시작 실패 후 잠금이 풀려 재시도가 가능하다', async () => {
    let calls = 0;
    const c = commands({startScrape: async () => { if (++calls === 1) throw Error('test'); }});
    await c.startScrape(); await c.startScrape();
    assert.equal(calls, 2);
});

test('수집 중에는 새 시작을 막고 중지 연속 클릭은 요청 1회만 보낸다', async () => {
    let stops = 0, starts = 0, finish;
    const c = commands({startScrape: async () => { starts++; }, stopScrape: () => { stops++; return new Promise(resolve => {finish = resolve;}); }}, true);
    await c.startScrape();
    const first = c.stopScrape(); await c.stopScrape();
    assert.equal(starts, 0); assert.equal(stops, 1);
    finish(); await first;
    assert.ok(c.states.some(([id, value]) => id === 2 && value === false));
});

test('수집 종료 시 홈 후보와 통합 새소식 목록 캐시를 함께 갱신한다', () => {
    const c = commands({}); c.invalidateData();
    assert.ok(c.invalidated.includes(queryKeys.notionCandidates));
    assert.ok(c.invalidated.includes(queryKeys.newsFeed));
    assert.match(home, /queryKey: queryKeys\.notionCandidates/);
    assert.match(news, /queryKey:\s*\[\.\.\.queryKeys\.newsFeed, scope.id, 'ready', offset\]/);
    assert.match(newsRecords, /queryKey:\[\.\.\.queryKeys\.newsFeed,profileId,view,offset\]/);
    assert.match(news, /welfareApi.getNewsFeed\(/);
});

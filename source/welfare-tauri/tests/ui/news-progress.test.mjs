import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1423';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,width=1280){
    const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{assert.deepEqual(await page.evaluate(()=>globalThis.__unexpected),[]);await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-progress.html');
    await page.locator('#sidebar-status').getByRole('progressbar').waitFor({timeout:30000});
    return page;
}
test('새 수집 진행은 기존 사이드바와 본문에서 표시하고 다른 화면에도 유지',async t=>{
    const page=await fixture(t),sidebar=page.locator('#sidebar-status');
    assert.equal(await sidebar.getByRole('progressbar').getAttribute('aria-valuenow'),'28');
    assert.equal(await page.getByRole('button',{name:'새 소식을 찾는 중…',exact:true}).isDisabled(),true);
    await page.locator('main').getByText('기관 확인 12/42개 사이트 · 28%',{exact:true}).waitFor();
    await page.getByRole('link',{name:'홈',exact:true}).click();
    await page.getByRole('heading',{name:'다른 화면 시험'}).waitFor();
    assert.equal(await sidebar.getByRole('progressbar').getAttribute('aria-valuenow'),'28');
    await page.evaluate(()=>globalThis.__setTask({progress:{schema_version:1,phase:'analysis',analysis_processed:18,analysis_total:30,analysis_remaining:12,analysis_review_required:2},stage:'analyzing'}));
    await sidebar.getByText('분석 처리 18/30건 · 60%',{exact:true}).waitFor();
    assert.equal(await sidebar.getByRole('link',{name:'새 소식에서 진행 확인'}).count(),0);
    await page.getByRole('link',{name:'새 소식',exact:true}).click();
    await page.locator('main').getByText('분석 처리 18/30건 · 60%',{exact:true}).waitFor();
});
test('활성 작업은 2초 폴링하고 본문 분모는 현재 기관 자료로 명시',async t=>{
    const page=await fixture(t),sidebar=page.locator('#sidebar-status');
    const before=await page.evaluate(()=>globalThis.__reads);
    // Update the synthetic server without invalidating queries: polling must pick it up.
    await page.evaluate(()=>{const p=globalThis.__task();p.progress={schema_version:1,phase:'content',sites_total:42,sites_processed:12,site_name:'교육기관',site_documents_total:20,site_documents_processed:5,documents_processed:30};globalThis.__serverTask(p);});
    await sidebar.getByText('이 기관 자료 5/20건 · 25%',{exact:true}).waitFor();
    await page.waitForFunction(before=>globalThis.__reads>before+2,before,{timeout:7000});
    assert.equal(await sidebar.getByText('현재 단계의 처리 비율입니다. 전체 작업 완료율·남은 시간과는 다릅니다.',{exact:true}).count(),0);
    assert.match(await sidebar.getByRole('progressbar').getAttribute('aria-valuetext'),/현재 단계 25%/);
});
test('할당량 대기는 사유·남은 수를 유지하고 활동 표시를 멈춤',async t=>{
    const page=await fixture(t),sidebar=page.locator('#sidebar-status');
    await page.evaluate(()=>globalThis.__setTask({state:'waiting',active:false,stage:'analyzing',error_code:'daily_quota_exhausted',progress:{schema_version:1,phase:'analysis',analysis_processed:18,analysis_total:30,analysis_remaining:12}}));
    await sidebar.getByText('새 소식 · AI 한도 대기',{exact:true}).waitFor();
    assert.equal(await sidebar.locator('.animate-pulse').count(),0);
    await sidebar.getByText('분석 처리 18/30건 · 60%',{exact:true}).waitFor();
});
test('연결 오류시 마지막 수치임을 명시, 미확정분량은 가짜백분율없음',async t=>{
    const page=await fixture(t),sidebar=page.locator('#sidebar-status');
    await page.evaluate(()=>globalThis.__setTask({progress:{schema_version:1,phase:'sites',sites_total:null,sites_processed:0}}));
    await sidebar.getByRole('progressbar').waitFor();
    await page.waitForFunction(()=>document.querySelector('#sidebar-status [role=progressbar]')?.getAttribute('aria-valuenow')===null);
    await page.evaluate(()=>globalThis.__failRead());
    await sidebar.getByText('새 소식 · 연결 확인 중',{exact:true}).waitFor();
    await sidebar.getByText(/^마지막 확인 ·/).waitFor();
    assert.equal(await sidebar.locator('.animate-pulse').count(),0);
});

test('불완전한 성공 응답은 진행을 지우거나 재시작을 허용하지 않고 정상 응답에서 회복한다',async t=>{
    const page=await fixture(t),main=page.locator('main'),sidebar=page.locator('#sidebar-status');
    await main.getByRole('button',{name:'이 작업 중지',exact:true}).waitFor();
    await page.evaluate(async()=>{
        const {welfareApi}=await import('/src/api/welfare.js');
        const names=['getNewsWorkflows','getNewsWorkflow','getDefaultNewsCondition'];
        globalThis.__validNewsReads=Object.fromEntries(names.map(name=>[name,welfareApi[name]]));
        welfareApi.getNewsWorkflows=async()=>({});welfareApi.getNewsWorkflow=async()=>({});
    });
    await main.getByRole('button',{name:'상태 새로고침',exact:true}).click();
    await main.getByRole('alert').filter({hasText:'최신 작업 상태를 읽지 못했습니다.'}).waitFor();
    await sidebar.getByText('새 소식 · 연결 확인 중',{exact:true}).waitFor();
    assert.equal(await main.getByRole('button',{name:'이 작업 중지',exact:true}).isVisible(),true);
    assert.equal(await main.getByRole('button',{name:'새 소식을 찾는 중…',exact:true}).isDisabled(),true);
    assert.equal(await main.getByRole('progressbar').getAttribute('aria-valuenow'),'28');
    assert.equal(await main.getByText('지역',{exact:true}).count(),1,'마지막 수집 조건도 유지');
    await page.evaluate(async()=>{const {welfareApi}=await import('/src/api/welfare.js');welfareApi.getDefaultNewsCondition=async()=>({});await globalThis.__failCondition();});
    await main.getByText(/수집 조건 업데이트를 확인하지 못했습니다/).waitFor();
    assert.equal(await main.getByRole('button',{name:'이 작업 중지',exact:true}).isVisible(),true);
    await page.evaluate(async()=>{const {welfareApi}=await import('/src/api/welfare.js');Object.assign(welfareApi,globalThis.__validNewsReads);});
    await page.evaluate(()=>globalThis.__restoreCondition());
    await main.getByRole('button',{name:'상태 새로고침',exact:true}).click();
    await main.getByRole('alert').waitFor({state:'hidden'});
    await sidebar.getByText('기관 확인 12/42개 사이트 · 28%',{exact:true}).waitFor();
});

test('조건 재조회 오류에도 수집 진행·중지와 기존 패널을 유지하고 복구한다',async t=>{
    const page=await fixture(t),main=page.locator('main');
    await main.getByRole('button',{name:'이 작업 중지',exact:true}).waitFor();
    const reads=await page.evaluate(()=>{
        globalThis.__mountedPanel=document.querySelector('[aria-label="대상별 수집 실행"]');
        return globalThis.__optionReads;
    });
    await page.evaluate(()=>globalThis.__failCondition());
    await main.getByText(/수집 조건 업데이트를 확인하지 못했습니다/).waitFor();
    assert.equal(await page.evaluate(()=>globalThis.__mountedPanel===document.querySelector('[aria-label="대상별 수집 실행"]')),true);
    assert.equal(await page.evaluate(()=>globalThis.__optionReads),reads);
    assert.equal(await main.getByRole('button',{name:'이 작업 중지',exact:true}).isVisible(),true);
    assert.equal(await main.getByRole('progressbar').getAttribute('aria-valuenow'),'28');
    await page.evaluate(()=>globalThis.__restoreCondition());
    await main.getByText(/수집 조건 업데이트를 확인하지 못했습니다/).waitFor({state:'hidden'});
    assert.equal(await page.evaluate(()=>globalThis.__optionReads),reads);
});

test('추가 확인을 남긴 완료는 소식 0건이어도 결과 요약을 유지하고 처리 기록으로 연결한다',async t=>{
    const page=await fixture(t),main=page.locator('main');
    await page.evaluate(()=>globalThis.__setTask({stage:'analyzing',progress:{schema_version:1,phase:'analysis',analysis_processed:6,analysis_total:22,analysis_remaining:16,analysis_review_required:4}}));
    await main.getByText('분석 처리 6/22건 · 27%',{exact:true}).waitFor();
    await page.evaluate(()=>globalThis.__setTask({state:'complete',active:false,stage:'finished',can_resume:false,
        summary:{analysis:{review_required_count:4}},
        progress:{schema_version:1,phase:'analysis',analysis_processed:22,analysis_total:22,analysis_remaining:0,analysis_review_required:4,sites_review_required:4}}));
    const result=main.getByLabel('최근 수집 결과',{exact:true});
    await result.getByText('최근 수집 · 수집 완료 · 일부 분석 보류',{exact:true}).waitFor();
    await result.getByText('분석 처리 22/22건 · 직접 확인 필요 4건 · 추가 확인 4개 사이트',{exact:true}).waitFor();
    await main.getByRole('heading',{name:'찾은 소식 0건',exact:true}).waitFor();
    assert.equal(await page.getByRole('progressbar').count(),0);
    assert.equal(await main.getByText('수집 이력',{exact:true}).evaluate(node=>node.parentElement.open),false);
    assert.equal(await result.count(),1);
    assert.equal((await page.evaluate(()=>globalThis.__feedReads)).includes('review'),false);
    await mkdir('../artifacts/news-progress-20260918',{recursive:true});
    await page.screenshot({path:'../artifacts/news-progress-20260918/completed-with-review-1280.png',fullPage:true});
    await result.getByRole('button',{name:'처리 기록 보기',exact:true}).click();
    await main.getByRole('button',{name:'추가 확인',exact:true}).waitFor();
    assert.equal(await main.getByRole('button',{name:'추가 확인',exact:true}).getAttribute('aria-pressed'),'true');
    await main.getByRole('button',{name:'확인이 필요한 자료 1',exact:true}).waitFor();
    await page.getByRole('link',{name:'홈',exact:true}).click();
    await page.getByRole('link',{name:'새 소식',exact:true}).click();
    await main.getByLabel('최근 수집 결과',{exact:true}).waitFor();
});

test('중지와 재개 불가 실패도 최근 결과에 이유를 남기며 과거 이력은 접어 둔다',async t=>{
    const page=await fixture(t),main=page.locator('main');
    await page.evaluate(()=>globalThis.__setTask({state:'cancelled',active:false,stage:'cancelled',can_resume:false}));
    await main.getByText('최근 수집 · 사용자가 중지함',{exact:true}).waitFor();
    assert.equal(await main.getByText('수집 이력',{exact:true}).evaluate(node=>node.parentElement.open),false);
    await page.evaluate(()=>globalThis.__setTask({state:'failed',active:false,stage:'failed',can_resume:false,error_code:'analysis_failed'}));
    await main.getByRole('button',{name:'상태 새로고침',exact:true}).click();
    await main.getByText('최근 수집 · 완료하지 못함',{exact:true}).waitFor();
    await main.getByLabel('최근 수집 결과',{exact:true}).getByText(/분석을 마치지 못한 자료/).waitFor();
    assert.equal(await main.getByLabel('최근 수집 결과',{exact:true}).count(),1);
});
test('밝은 기존 표시 스타일과 좁은 창에서도 진행 정보를 확인',async t=>{
    await mkdir('../artifacts/news-progress-20260917',{recursive:true});
    for(const width of [850,1280]){
        const page=await fixture(t,width);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:`../artifacts/news-progress-20260917/progress-${width}.png`});
    }
});

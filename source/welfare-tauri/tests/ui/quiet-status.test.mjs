import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1428';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function open(t,path,width=582){
    const context=await browser.newContext({viewport:{width,height:950},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>{const u=new URL(r.request().url());return u.origin===origin&&!u.pathname.startsWith('/api/')?r.continue():r.abort();});
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+path);return page;
}
for(const state of ['waiting','running'])test(`${state} 숨김·취소·재진입·복원은 작업을 중단/재개/삭제하지 않음`,async t=>{
    const page=await open(t,`/tests/ui/news-history.html?task=${state}`);
    const hide=page.getByRole('button',{name:'숨기기',exact:true});await hide.waitFor();
    const primary=page.getByRole('button',{name:state==='running'?'새 소식을 찾는 중…':'기존 작업을 이어서 진행하거나 종료해주세요',exact:true});
    assert.equal(await primary.isDisabled(),true);
    page.once('dialog',d=>d.dismiss());await hide.click();assert.equal(await hide.count(),1);
    page.once('dialog',d=>{assert.match(d.message(),/계속 실행/);return d.accept();});await hide.click();
    await page.getByText('숨긴 작업 1건 · 보기',{exact:true}).waitFor();assert.equal(await hide.count(),0);
    assert.equal(await primary.isDisabled(),true);
    await page.reload();await page.getByText('숨긴 작업 1건 · 보기',{exact:true}).waitFor();
    assert.equal(await hide.count(),0);assert.equal(await primary.isDisabled(),true);
    await page.getByText('숨긴 작업 1건 · 보기',{exact:true}).click();await page.getByText('작업 보기',{exact:true}).click();
    await page.getByRole('button',{name:state==='running'?'이 작업 중지':'이어서 진행',exact:true}).waitFor();
    await page.getByRole('button',{name:'다시 표시',exact:true}).click();await hide.waitFor();
    assert.deepEqual(await page.evaluate(()=>globalThis.__clears),[]);assert.deepEqual(await page.evaluate(()=>globalThis.__unexpected),[]);
});
test('정책 공통 경고는 한 번만, 상세 사유는 펼칠 때만 표시하며 반영 차단 유지',async t=>{
    await mkdir('../artifacts/quiet-status-20260917',{recursive:true});
    for(const width of [582,1280]){
        const page=await open(t,'/tests/ui/policy-review.html?mode=compact-notices',width);
        const card=page.locator('#policy-review-1');await card.waitFor();
        assert.equal(await page.getByText(/재점검이 필요한 제안은 위의/).count(),1);
        assert.equal(await page.getByText(/저장된 제안은 보존했습니다/).count(),0);
        assert.equal(await page.getByText(/원문이 길어 분석을 완료하지 못했습니다/).count(),0);
        assert.match(await card.innerText(),/상세 출처 연결 필요/);
        await page.screenshot({path:`../artifacts/quiet-status-20260917/policy-${width}.png`});
        await card.locator('button[aria-controls="policy-detail-1"]').click();
        assert.equal(await card.getByText(/원문이 길어 분석을 완료하지 못했습니다/).count(),1);
        assert.doesNotMatch(await card.innerText(),/다음 단계:/);
        assert.equal(await card.getByRole('button',{name:/^(바로 적용|반영할 내용 선택)$/}).count(),0);
        assert.equal(await page.getByRole('button',{name:'선택한 내용 Notion에 반영 (0)',exact:true}).isDisabled(),true);
        assert.deepEqual(await page.evaluate(()=>window.policyCalls),[]);assert.deepEqual(await page.evaluate(()=>window.individualCalls),[]);
        assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
    }
});
test('반영 결과 미확인 상태는 짧은 경고를 유지하고 상세에서 복구 안내를 제공',async t=>{
    const page=await open(t,'/tests/ui/policy-review.html?mode=history-uncertain');
    const card=page.locator('#policy-review-1');await card.waitFor();
    assert.match(await card.innerText(),/반영 확인 필요/);
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    await card.getByText('아래 반영·복구 기록과 Notion의 현재 내용을 먼저 확인해주세요.',{exact:false}).waitFor();
    assert.equal(await card.getByRole('button',{name:/^(바로 적용|반영할 내용 선택)$/}).count(),0);
    assert.equal(await page.getByText(/재점검이 필요한 제안은 위의/).count(),0);
});

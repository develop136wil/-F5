import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1422';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,mode){
    const context=await browser.newContext(),page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-recovery.html?mode='+mode);
    await page.getByText('처리 기록',{exact:true}).click();
    await page.getByRole('button',{name:'이전 연결 실패',exact:true}).waitFor();
    return page;
}
test('구형 실패는 없는 재개 작업을 약속하지 않고 설정으로만 안내한다',async t=>{
    const page=await fixture(t,'legacy');
    assert.equal(await page.getByRole('button',{name:'수집 진행으로 이동',exact:true}).count(),0);
    await page.getByText(/바로 재개할 연결 작업이 없습니다/).waitFor();
    const ai=page.getByRole('link',{name:'AI 연결 확인',exact:true});
    assert.equal(await ai.getAttribute('href'),'/settings?tab=guide&step=google');
    await page.getByRole('link',{name:'수집 조건 확인·저장',exact:true}).click();
    assert.equal(await page.getByLabel('이동 경로').textContent(),'/settings?tab=keywords');
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.unexpected),[]);
});
test('실제 연결 작업이 있을 때만 기존 수집 진행으로 이동한다',async t=>{
    const page=await fixture(t,'workflow');
    assert.equal(await page.getByRole('link',{name:'수집 조건 확인·저장',exact:true}).count(),0);
    await page.getByRole('button',{name:'수집 진행으로 이동',exact:true}).click();
    assert.equal(await page.evaluate(()=>document.activeElement.id),'news-collection');
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.unexpected),[]);
});

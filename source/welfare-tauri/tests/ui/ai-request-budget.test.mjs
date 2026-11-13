// Synthetic Settings fixture only: no live backend, keys, DB, or AI requests.
import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1436';
const output='../artifacts/distribution-hardening-20260909/ui';
let browser;
before(async()=>{browser=await chromium.launch({headless:true,channel:process.env.UI_TEST_BROWSER||'msedge'});await mkdir(output,{recursive:true});});
after(async()=>{await browser?.close();});
async function fixture(t,width){
    const context=await browser.newContext({viewport:{width,height:1050},serviceWorkers:'block'});
    const page=await context.newPage(),errors=[],blocked=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{
        const url=new URL(route.request().url());
        if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();
        blocked.push(url.origin+url.pathname);return route.abort();
    });
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);});
    await page.goto(`${origin}/tests/ui/settings-drafts.html`,{timeout:30000});
    await page.locator('summary').filter({hasText:'AI 사용량 보호'}).waitFor();
    return page;
}
test('한도 편집 없이 간결한 안내와 키보드로 펼치는 처리 설명 제공',async t=>{
    for(const width of [390,850]){
        const page=await fixture(t,width);
        const settings=page.getByRole('tabpanel',{name:'연결 관리',exact:true});
        const before=await page.evaluate(()=>globalThis.__fixtureReadConfig());
        assert.equal(await settings.getByLabel('요청 상한 (회)',{exact:true}).count(),0);
        assert.equal(await settings.getByRole('button',{name:'상한 저장',exact:true}).count(),0);
        assert.equal(await settings.getByText('프로젝트 한도 확인',{exact:true}).count(),0);
        const summary=settings.locator('summary').filter({hasText:'AI 사용량 보호'});
        const details=summary.locator('..');
        assert.equal(await details.getAttribute('open'),null);
        await summary.focus();await page.keyboard.press('Enter');
        assert.notEqual(await details.getAttribute('open'),null);
        assert.match(await details.innerText(),/Google Gemini API에서 처리됩니다/);
        assert.match(await details.innerText(),/중복 요청과 과도한 재시도를 방지하고 요청 간격을 조절합니다/);
        assert.doesNotMatch(await details.innerText(),/한도는 앱에서 관리|20회|일일 상한/);
        const paid=settings.getByRole('link',{name:'Google 유료 이용 안내 ↗',exact:true});
        await paid.focus();
        await page.getByRole('tooltip').filter({hasText:'유료 프로젝트는 사용량에 따라 비용이 발생할 수 있습니다.'}).waitFor();
        assert.equal(await details.getByRole('link').getAttribute('href'),'https://ai.google.dev/gemini-api/terms');
        for(const dark of [false,true]){
            await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            assert.equal(await settings.evaluate(el=>el.scrollWidth<=el.clientWidth&&document.documentElement.scrollWidth<=window.innerWidth),true);
        }
        assert.deepEqual(await page.evaluate(()=>globalThis.__fixtureReadConfig()),before);
        assert.deepEqual(await page.evaluate(()=>globalThis.__fixtureConfigSaves),[]);
    }
});

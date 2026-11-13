import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=fileURLToPath(new URL('../../',import.meta.url));
const output=fileURLToPath(new URL('../../../artifacts/news-long-details-20260913/ui/',import.meta.url));

test('긴 분석은 펼칠 때만 조회·마운트하고 늦은 응답·오류·행 변경을 안전하게 처리한다',{timeout:120000},async()=>{
    const server=await createServer({root,envDir:false,envPrefix:[],server:{host:'127.0.0.1',port:0,strictPort:false,watch:null,hmr:false}});
    let browser;
    try{
        await server.listen();const origin=`http://127.0.0.1:${server.httpServer.address().port}`;
        browser=await chromium.launch({channel:'msedge',headless:true});await mkdir(output,{recursive:true});
        for(const width of [390,1280]){
            const page=await browser.newPage({viewport:{width,height:900},serviceWorkers:'block'});
            const errors=[],blocked=[];page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();blocked.push(url.href);return route.abort();});
            await page.goto(`${origin}/tests/ui/news-result-details.html`);
            const summaries=page.locator('summary');await summaries.first().waitFor();
            assert.deepEqual(await page.evaluate(()=>window.calls),[]);
            assert.equal(await page.locator('dd').count(),0);
            await summaries.first().focus();await page.keyboard.press('Enter');
            await page.getByRole('status').waitFor();
            await summaries.first().click();
            assert.equal(await page.locator('dd').count(),0);
            await page.getByRole('button',{name:'응답 도착',exact:true}).click();
            assert.equal(await page.locator('dd').count(),0);
            assert.equal(await page.evaluate(()=>window.calls[0].aborted),true);
            await summaries.first().click();await page.getByRole('status').waitFor();
            await page.getByRole('button',{name:'응답 실패',exact:true}).click();
            await page.getByRole('alert').waitFor();
            await page.getByRole('button',{name:'다시 불러오기',exact:true}).click();
            await page.getByRole('status').waitFor();
            await page.getByRole('button',{name:'응답 도착',exact:true}).click();
            const region=page.getByRole('region',{name:'지원 내용 전체 내용',exact:true});await region.waitFor();
            assert.match(await region.textContent(),/마지막 조건: 중복 지원은 제외합니다/);
            assert.equal(await region.locator('script').count(),0);
            assert.equal(await page.evaluate(()=>window.untrustedExecuted),undefined);
            await region.focus();await page.keyboard.press('End');
            assert.ok(await region.evaluate(el=>el.scrollHeight>el.clientHeight));
            for(const theme of ['light','dark']){
                await page.evaluate(theme=>{document.documentElement.classList.toggle('dark',theme==='dark');document.documentElement.style.fontSize='20px';},theme);
                assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
                await page.screenshot({path:`${output}/details-${width}-${theme}.png`,animations:'disabled'});
            }
            await page.getByRole('button',{name:'동일 기록 갱신',exact:true}).click();await page.getByRole('status').waitFor();
            assert.equal(await page.locator('dd').count(),0);
            await page.getByRole('button',{name:'다른 기록',exact:true}).click();await page.getByRole('status').waitFor();
            await page.getByRole('button',{name:'응답 도착',exact:true}).click(); // stale same-record reply
            assert.equal(await page.locator('dd').count(),0);
            await page.getByRole('button',{name:'응답 도착',exact:true}).click();await region.waitFor();
            assert.equal((await page.evaluate(()=>window.calls)).at(-1).profileId,'other');
            await summaries.first().click();await page.locator('dd').waitFor({state:'detached'});
            assert.equal(await page.locator('dd').count(),0);
            const calls=await page.evaluate(()=>window.calls.length);
            await summaries.nth(1).click();await page.getByText('짧은 공식 내용',{exact:true}).waitFor();
            await summaries.nth(2).click();await page.getByText(/화면 표시 한도를 넘어 생략했습니다/).waitFor();
            assert.equal(await page.evaluate(()=>window.calls.length),calls);
            assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);await page.close();
        }
    }finally{if(browser)await browser.close();await server.close();}
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=fileURLToPath(new URL('../../',import.meta.url));
const output=fileURLToPath(new URL('../../../artifacts/ai-release-upgrade-20260913/ui/',import.meta.url));

test('영속 보류 UI: 페이지·재진입·오류·키보드·좁은 화면을 읽기 전용으로 검사', {timeout:120000}, async()=>{
    const server=await createServer({root,configFile:false,envFile:false,server:{host:'127.0.0.1',port:0,strictPort:false,watch:{ignored:['**/src-tauri/**','**/build/**','**/dist/**']},hmr:false}});
    let browser;
    try {
        await server.listen();
        const origin=`http://127.0.0.1:${server.httpServer.address().port}`;
        browser=await chromium.launch({channel:'msedge',headless:true});
        await mkdir(output,{recursive:true});
        for(const width of [390,1280]) {
            const page=await browser.newPage({viewport:{width,height:900},serviceWorkers:'block'});
            const errors=[],blocked=[];
            page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',r=>{const url=new URL(r.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return r.continue();blocked.push(url.href);return r.abort();});
            await page.goto(`${origin}/tests/ui/policy-review-queue.html`,{timeout:60000});
            const summary=page.getByText('변경 제안 없이 확인이 필요한 정책 · 보류 사유 53건',{exact:true});
            await summary.waitFor();await summary.focus();await page.keyboard.press('Enter');
            const queue=page.getByRole('region',{name:'저장된 정책 보류 기록'});
            assert.equal(await queue.locator('li').count(),50);
            assert.equal(await queue.locator('a[href^="javascript:"]').count(),0);
            assert.equal(await queue.getByRole('link').count(),49);
            await queue.getByRole('button',{name:'다음 50건',exact:true}).click();
            await queue.getByRole('heading',{name:'정책 51 · 출생일과 나이 기준',exact:true}).waitFor();
            assert.equal(await queue.locator('li').count(),3);
            assert.equal(await queue.getByRole('button',{name:'다음 50건',exact:true}).isDisabled(),true);
            for(const theme of ['light','dark']) {
                await page.evaluate(t=>{document.documentElement.classList.toggle('dark',t==='dark');document.documentElement.style.fontSize='20px';},theme);
                assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
                const box=await queue.getByRole('button',{name:'보류 기록 새로고침',exact:true}).boundingBox();
                assert.ok(box.height>=44);
                await page.screenshot({path:`${output}/policy-queue-${width}-${theme}.png`,animations:'disabled'});
            }
            await page.getByRole('button',{name:'다른 정책표 선택',exact:true}).click();
            await queue.waitFor({state:'hidden'});
            await page.getByRole('button',{name:'다른 정책표 선택',exact:true}).click();
            await summary.waitFor();await summary.click();
            await queue.getByRole('heading',{name:'정책 1 · 출생일과 나이 기준',exact:true}).waitFor();
            await page.reload();await summary.waitFor();
            await page.goto(`${origin}/tests/ui/policy-review-queue.html?mode=error`);
            await page.getByRole('alert').waitFor();
            assert.match(await page.getByRole('alert').innerText(),/없다는 뜻은 아닙니다/);
            assert.equal(await page.getByText('변경 제안 없이 확인이 필요한 정책 · 보류 사유 0건',{exact:true}).count(),0);
            await page.evaluate(()=>{window.fixtureFailure=false;});
            await page.getByRole('button',{name:'보류 기록 새로고침',exact:true}).click();
            await summary.waitFor();
            assert.ok((await page.evaluate(()=>window.fixtureCalls)).every(call=>call.method==='getPolicyReviews'));
            assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
            await page.close();
        }
    } finally {if(browser)await browser.close();await server.close();}
});

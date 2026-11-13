// Isolated fixture and synthetic counters; all external/API requests blocked.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1436';
test('화면 이미지 확인 경고는 0건과 구별하며 완료 수집을 자동 재개하지 않는다',async()=>{
    const browser=await chromium.launch({...(process.env.UI_BROWSER_EXECUTABLE?{executablePath:process.env.UI_BROWSER_EXECUTABLE}:{channel:'msedge'}),headless:true});
    const output=process.env.UI_TEST_ARTIFACT_DIR||'../artifacts/distribution-hardening-20260909/ui';
    await mkdir(output,{recursive:true});
    try{
        for(const width of [390,850]){
            const page=await browser.newPage({viewport:{width,height:1000},serviceWorkers:'block'});
            const errors=[],blocked=[];
            page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*',route=>{
                const url=new URL(route.request().url());
                if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();
                blocked.push(url.origin+url.pathname);return route.abort();
            });
            await page.goto(`${origin}/tests/ui/news-run.html?mode=vision`,{timeout:30000});
            await page.getByText('처리 내역',{exact:true}).click();
            const warning=page.getByText('2개 사이트에서 화면 이미지의 추가 분석은 직접 확인이 필요합니다.',{exact:false});
            await warning.waitFor();
            assert.match(await warning.innerText(),/개인정보 확인 전에는 화면을 AI로 보내지 않습니다.*찾은 링크는 보존.*새 소식이 없다는 뜻은 아닙니다/);
            assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
            for(const dark of [false,true]){
                await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
                assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
                await page.locator('main').screenshot({path:`${output}/news-vision-review-${dark?'dark':'light'}-${width}.png`,animations:'disabled'});
            }
            await page.evaluate(()=>globalThis.__fixtureSetVisionReview(0));
            await page.getByRole('button',{name:'상태 새로고침',exact:true}).click();
            await warning.waitFor({state:'detached'});
            assert.equal(await page.evaluate(()=>globalThis.__fixtureNewsRunWrites),0);
            assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
            await page.close();
        }
    }finally{await browser.close();}
});

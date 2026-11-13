import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=fileURLToPath(new URL('../../',import.meta.url));
const output=fileURLToPath(new URL('../../../artifacts/ai-release-upgrade-20260913/ui/',import.meta.url));

test('읽기 전용 처리 기록은 원문 범위·원문 대조를 보존하고 별도 가져오기 없이 찾은 소식으로 안내한다',{timeout:120000},async()=>{
    const server=await createServer({root,envDir:false,envPrefix:[],server:{host:'127.0.0.1',port:0,strictPort:false,watch:null,hmr:false}});
    let browser;
    try {
        await server.listen();const origin=`http://127.0.0.1:${server.httpServer.address().port}`;
        browser=await chromium.launch({channel:'msedge',headless:true});await mkdir(output,{recursive:true});
        for(const width of [390,1280]) {
            const page=await browser.newPage({viewport:{width,height:1000},serviceWorkers:'block'});
            const errors=[],blocked=[];page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',r=>{const url=new URL(r.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return r.continue();blocked.push(url.href);return r.abort();});
            await page.goto(`${origin}/tests/ui/news-result-readiness.html`,{timeout:60000});
            await page.getByText('제공기관 모집',{exact:true}).waitFor();
            const provider=page.locator('li').filter({has:page.getByText('제공기관 모집',{exact:true})});
            assert.equal(await provider.getByRole('button',{name:'새 소식 후보로 가져오기',exact:true}).count(),0);
            assert.match(await provider.innerText(),/검토 필요/);
            assert.equal(await page.getByRole('button',{name:'새 소식 후보로 가져오기',exact:true}).count(),0);
            assert.equal(await page.getByRole('link',{name:'찾은 소식으로 이동',exact:true}).getAttribute('href'),'/notion?profile=fixture');
            await page.getByText('결과 표시 도움말',{exact:true}).click();
            await page.getByText('저장·등록은 ‘찾은 소식’에서 필요한 항목을 선택해 진행하세요. 별도로 가져오는 단계는 없습니다.',{exact:true}).waitFor();
            await provider.getByText('원문 대조 안내',{exact:true}).focus();await page.keyboard.press('Enter');
            await provider.getByText(/동일 사업 중복 신청 불가/).waitFor();
            assert.equal(await page.evaluate(()=>window.untrustedExecuted),undefined);
            assert.equal(await provider.locator('script').count(),0);
            await page.getByText(/확보한 내용 기준 일치 · 원문 확인 필요/).waitFor();
            await page.getByText(/본문을 확보하지 못함 · 검토 필요/).waitFor();
            const long=page.locator('li').filter({has:page.getByText('긴 원문 확인 자료',{exact:true})});
            await long.getByText('원문 대조 안내',{exact:true}).click();
            await long.getByText('이 밖의 확인할 내용은 원문에서 직접 확인해주세요.',{exact:true}).waitFor();
            for(const theme of ['light','dark']) {
                await page.evaluate(t=>{document.documentElement.classList.toggle('dark',t==='dark');document.documentElement.style.fontSize='20px';},theme);
                assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
                await page.screenshot({path:`${output}/news-results-${width}-${theme}.png`,animations:'disabled'});
            }
            assert.deepEqual(await page.evaluate(()=>window.fixtureCalls),['getNewsProfileResults']);
            assert.equal(await page.getByRole('dialog').count(),0);
            assert.equal(await page.getByRole('button',{name:'새 소식 후보로 가져오기',exact:true}).count(),0);
            assert.deepEqual(await page.evaluate(()=>window.fixtureCalls),['getNewsProfileResults'],'기록 조회·도움말·원문 대조 펼치기는 저장이나 재분석을 요청하지 않음');
            assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);await page.close();
        }
    }finally{if(browser)await browser.close();await server.close();}
});

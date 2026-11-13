import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const artifact='../artifacts/news-readiness-20260909';
async function open(browser,mode,dark=false){
    const page=await browser.newPage({viewport:{width:390,height:844},colorScheme:dark?'dark':'light'});
    page.errors=[];page.external=[];
    page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();page.external.push(url.origin);return route.abort();});
    await page.goto(`${origin}/tests/ui/news-collection.html?mode=${mode}`);
    if(dark)await page.evaluate(()=>document.documentElement.classList.add('dark'));
    await page.getByRole('heading',{name:'수집 범위',exact:true}).waitFor();
    return page;
}
async function safe(page){
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
    assert.deepEqual(page.external,[]);assert.deepEqual(page.errors,[]);
}

test('현재 조건 원문 수집은 AI 키·모델·한도·상태 조회와 분리되고 확인 후 한 번 시작한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});await mkdir(artifact,{recursive:true});
    try{
        for(const mode of ['no-key','no-model','provider-limit','usage-error','ai-loading','ai-error','ai-busy']){
            const page=await open(browser,mode,mode==='ai-busy');
            await page.getByText('AI 분석 준비:',{exact:false}).waitFor();
            const start=page.getByRole('button',{name:'새 소식 찾기',exact:true});
            assert.equal(await start.isEnabled(),true,mode);
            if(mode==='no-key'||mode==='ai-busy')await page.screenshot({path:`${artifact}/${mode}-390.png`,fullPage:true});
            await start.focus();await page.keyboard.press('Enter');
            assert.deepEqual(await page.evaluate(()=>window.starts),[]);
            await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
            await page.waitForFunction(()=>window.starts.length===1);
            assert.deepEqual(await page.evaluate(()=>window.starts),[['default-news',1]]);
            await safe(page);await page.close();
        }
        const page=await open(browser,'snapshot-error');
        await page.getByRole('alert').waitFor();
        assert.equal(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).isDisabled(),true);
        assert.deepEqual(await page.evaluate(()=>window.starts),[]);await safe(page);
    }finally{await browser.close();}
});

test('미완료 원문 수집 재개는 AI 준비와 무관하며 수집 대기 중 새 회차는 차단한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        for(const mode of ['collecting-no-key','collecting-ai-busy']){
            const page=await open(browser,mode);
            const resume=page.getByRole('button',{name:'이어서 진행',exact:true});
            assert.equal(await resume.isEnabled(),true);
            assert.equal(await page.getByRole('button',{name:'기존 작업을 이어서 진행하거나 종료해주세요',exact:true}).isDisabled(),true);
            await resume.click();await page.waitForFunction(()=>window.resumes.length===1);
            assert.deepEqual(await page.evaluate(()=>window.resumes),[mode]);await safe(page);await page.close();
        }
        for(const mode of ['new-collecting-conflict','new-running-conflict']){
            const page=await open(browser,mode);
            assert.equal(await page.getByRole('button',{name:'새 원문 수집',exact:true}).count(),0);
            assert.equal(await page.getByRole('button',{name:mode==='new-running-conflict'?'새 소식을 찾는 중…':'기존 작업을 이어서 진행하거나 종료해주세요',exact:true}).isDisabled(),true);
            assert.deepEqual(await page.evaluate(()=>window.starts),[]);await safe(page);await page.close();
        }
    }finally{await browser.close();}
});

test('수집 완료 분석 대기는 보존하고 명시 확인한 새 원문 회차만 한 번 요청한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});await mkdir(artifact,{recursive:true});
    try{
        const page=await open(browser,'new-collection');
        await page.getByText('원문 수집 완료 · AI 분석 대기',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).isDisabled(),true,'준비되지 않은 AI 분석을 실행할 수 있다고 안내하지 않음');
        await page.getByRole('button',{name:'새 원문 수집',exact:true}).click();
        await page.getByText('이전 회차의 원문과 분석 대기는 그대로 보관하고,',{exact:false}).waitFor();
        assert.deepEqual(await page.evaluate(()=>window.starts),[]);
        await page.screenshot({path:`${artifact}/new-collection-390.png`,fullPage:true});
        const confirm=page.getByRole('button',{name:'확인하고 새 원문 수집',exact:true});
        await confirm.evaluate(button=>{button.click();button.click();});
        await page.waitForFunction(()=>window.starts.length===1);
        assert.equal(await confirm.isDisabled(),true);
        assert.deepEqual(await page.evaluate(()=>window.starts),[['default-news',1,{newCollection:true}]]);
        assert.deepEqual(await page.evaluate(()=>window.cancels),[]);
        await page.evaluate(()=>window.finishStart());
        await page.getByRole('button',{name:'새 원문 수집',exact:true}).waitFor();
        // Results now use one shared found-news list, not a second per-run list.
        await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).click();
        assert.deepEqual(await page.evaluate(()=>window.resultVisits),[['default-news','기본 수집']]);
        const retained=await page.evaluate(()=>window.readFixtureWorkflows().find(item=>item.workflow_id==='old-analysis'));
        assert.equal(retained.run_id,'old-source');
        assert.equal(retained.state,'waiting');
        assert.equal(retained.summary.analysis.remaining,7);
        assert.equal(await page.getByRole('button',{name:'새 소식 후보로 가져오기',exact:true}).count(),0);
        await safe(page);
    }finally{await browser.close();}
});

test('홈에서 AI 없이 새 소식으로 이동하여 현재 조건 원문 수집을 시작할 수 있다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:390,height:844}});
        const external=[],errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();external.push(url.origin);return route.abort();});
        await page.goto(`${origin}/tests/ui/home-workflow.html?mode=collection-no-ai`);
        const main=page.locator('#fixture-app main');
        await main.getByText('AI 분석을 위한 Gemini 연결 (선택)',{exact:false}).waitFor();
        await main.getByText('원문 수집은 AI 연결 없이 시작할 수 있습니다.',{exact:false}).waitFor();
        await main.getByRole('link',{name:'새 소식 탐색하기',exact:true}).click();
        await main.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await main.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.workflowStarts.length===1);
        assert.deepEqual(await page.evaluate(()=>window.workflowStarts),[['default-news',1]]);
        assert.equal(await main.evaluate(el=>el.scrollWidth>el.clientWidth),false);
        assert.deepEqual(external,[]);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
});

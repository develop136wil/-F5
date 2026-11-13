import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir,mkdtemp} from 'node:fs/promises';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const artifacts=process.env.UI_TEST_ARTIFACT_DIR||process.env.ARTIFACT_DIR||'../artifacts/news-first-use-acceptance';
async function open(browser,mode){
    const page=await browser.newPage({viewport:{width:1000,height:900}});
    page.errors=[];page.blocked=[];
    page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());
        if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();
        page.blocked.push(url.origin+url.pathname);return route.abort();});
    await page.goto(`${origin}/tests/ui/news-first-use.html?mode=${mode}`);
    return page;
}
async function safe(page){
    assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).unexpected,[]);
    assert.deepEqual(page.errors,[]);assert.deepEqual(page.blocked,[]);
}
async function waitEnabled(page){
    await page.waitForFunction(()=>[...document.querySelectorAll('button')]
        .some(button=>button.textContent.trim()==='새 소식 찾기'&&!button.disabled));
}

test('새 사용자 키 유무와 관계없이 명시 조건 저장 뒤에만 current 수집을 시작한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{for(const mode of ['fresh','fresh-no-key','preserved']){
        const page=await open(browser,mode);
        await page.getByText('수집 범위·기간을 먼저 저장해주세요.',{exact:false}).waitFor();
        assert.equal(await page.getByText('만 9세까지',{exact:true}).count(),0);
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('[data-testid="route"]').textContent==='/settings?tab=keywords');
        let calls=await page.evaluate(()=>window.firstUseCalls());
        assert.equal(calls.legacyStarts,0);assert.deepEqual(calls.starts,[]);assert.deepEqual(calls.saves,[]);
        await page.getByRole('region',{name:'새 수집 조건 만들기',exact:true}).waitFor();
        assert.equal(await page.getByLabel('시·도',{exact:true}).inputValue(),mode==='preserved'?'부산광역시':'전국');
        for(let step=0;step<3;step++)await page.getByRole('button',{name:'다음',exact:true}).click();
        await page.getByRole('button',{name:'최종 저장',exact:true}).click();
        await waitEnabled(page);
        calls=await page.evaluate(()=>window.firstUseCalls());
        assert.equal(calls.saves.length,1);assert.equal(calls.legacyStarts,0);assert.deepEqual(calls.starts,[]);
        assert.equal(calls.saves[0].region,mode==='preserved'?'부산광역시':'전국');
        assert.equal(calls.saves[0].max_age_months,mode==='preserved'?144:null);
        if(mode==='preserved'){
            assert.deepEqual(calls.saves[0].keywords,[]);
            assert.deepEqual(calls.saves[0].excluded_keywords,[]);
            assert.equal(calls.saves[0].ai_prompt,'기존 문구 보존');
        }
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.firstUseCalls().starts.length===1);
        assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).starts,[['default-news',1]]);
        await safe(page);await page.close();
    }}finally{await browser.close();}
});

test('사이트 등록 완료와 동일 profile revision 변경은 수동 새로고침 없이 반영된다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir(artifacts,{recursive:true});const run=await mkdtemp(path.join(artifacts,'readiness-'));
    try{for(const [mode,update] of [['registration-revision',{parent:true,invalidate:false}],
            ['registration-sites',{parent:false,invalidate:true}],['registration-background',{parent:false,invalidate:false}]]){
        const page=await open(browser,mode);
        await page.getByText('연결된 수집 사이트가 없습니다.',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).isDisabled(),true);
        if(update.parent)await page.evaluate(()=>{window.holdNextOptions=true;});
        await page.evaluate(value=>window.completeRegistration(value),update);
        if(update.parent){
            await page.waitForFunction(()=>typeof window.releaseOptions==='function');
            assert.equal(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).isDisabled(),true);
            assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).starts,[]);
            await page.evaluate(()=>window.releaseOptions());
        }
        await page.waitForFunction(()=>[...document.querySelectorAll('button')]
            .some(button=>button.textContent.trim()==='새 소식 찾기'&&!button.disabled),null,{timeout:15000});
        assert.equal(await page.getByText('연결된 수집 사이트가 없습니다.',{exact:true}).count(),0);
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.firstUseCalls().starts.length===1);
        assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).starts,[['default-news',2]]);
        await page.screenshot({path:path.join(run,`${mode}.png`),fullPage:true});
        await safe(page);await page.close();
    }}finally{await browser.close();}
});

test('확인 도중 바뀐 revision은 이전 조건으로 start하지 않고 최신 조건 재확인을 요구한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await open(browser,'changed-before-confirm');await waitEnabled(page);
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.evaluate(()=>window.completeRegistration({parent:false,invalidate:false}));
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.getByRole('alert').filter({hasText:'수집 조건이 변경되었습니다.'}).waitFor();
        assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).starts,[]);
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.firstUseCalls().starts.length===1);
        assert.deepEqual((await page.evaluate(()=>window.firstUseCalls())).starts,[['default-news',2]]);
        await safe(page);
    }finally{await browser.close();}
});

test('기존 사용자 legacy 사용과 이미 진행 중인 legacy의 중지 동선을 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const legacy=await open(browser,'legacy');
        await legacy.getByText('만 9세까지',{exact:true}).waitFor();
        assert.equal(await legacy.getByText('수집 범위·기간을 먼저 저장해주세요.',{exact:false}).count(),0);
        await safe(legacy);
        const running=await open(browser,'legacy-running');
        await running.getByRole('button',{name:'찾기 중지',exact:true}).waitFor();
        await running.evaluate(()=>window.createConditionDuringLegacy());
        await running.getByRole('button',{name:'찾기 중지',exact:true}).click();
        await running.waitForFunction(()=>window.firstUseCalls().stops===1);
        assert.deepEqual((await running.evaluate(()=>window.firstUseCalls())).starts,[]);
        await safe(running);
    }finally{await browser.close();}
});

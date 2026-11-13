import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
test('새 소식 조건·실행·조회 실패를 외부 호출 없이 검증', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/news-collection-20260907',{recursive:true});
    try {
        for(const width of [582,850,1280]){
            const page=await browser.newPage({viewport:{width,height:800}});
            const errors=[]; page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
            for(const mode of ['idle','busy','error','running']){
                await page.goto(`http://127.0.0.1:1420/tests/ui/news-collection.html?mode=${mode}`);
                await page.getByRole('heading',{name:'수집 범위',exact:true}).waitFor();
                if(mode==='error') await page.getByText('수집 조건을 확인하지 못했습니다.',{exact:true}).waitFor();
                else {
                    await page.getByText('서울특별시 도봉구',{exact:true}).waitFor();
                    await page.getByText('만 9세까지',{exact:true}).waitFor();
                    assert.equal(await page.getByText('영유아·아동 중심',{exact:false}).count(),0);
                }
                if(mode==='running') await page.getByRole('button',{name:'찾기 중지',exact:true}).waitFor();
                else {
                    const start=page.getByRole('button',{name:'새 소식 찾기',exact:true});
                    if(mode==='idle') await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='새 소식 찾기'&&!b.disabled));
                    assert.equal(await start.isDisabled(),mode!=='idle');
                }
                assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
                assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
                await page.screenshot({path:`../artifacts/news-collection-20260907/${mode}-${width}.png`});
            }
            assert.deepEqual(errors,[]); await page.close();
        }
    }finally{await browser.close();}
});

test('새 기본 조건이 준비되지 않으면 이전 영유아 수집으로 대신 실행하지 않는다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage();
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=default');
        const start=page.getByRole('button',{name:'새 소식 찾기',exact:true});
        await start.waitFor();
        assert.equal(await start.isDisabled(),true);
        assert.equal(await page.getByText('만 9세까지',{exact:true}).count(),0);
        assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
    }finally{await browser.close();}
});

test('기관 미등록 화면은 기존 수집 범위 배치에서 사이트 등록으로 직접 안내한다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:582,height:900}});
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=no-sites');
        await page.getByText('공지사항·모집 게시판을 먼저 등록해주세요.',{exact:true}).waitFor();
        const sites=page.getByRole('link',{name:'수집 사이트 등록·관리',exact:true});
        assert.equal(await sites.getAttribute('href'),'/sites');
        const start=page.getByRole('button',{name:'새 소식 찾기',exact:true});
        assert.equal(await start.isDisabled(),true);
        assert.match(await start.getAttribute('class'),/bg-teal-700/);
        assert.ok((await start.boundingBox()).y > (await sites.boundingBox()).y);
        assert.equal(await page.getByRole('link',{name:'설정 도우미',exact:true}).count(),0);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
    } finally {await browser.close();}
});

test('새 기본 조건은 저장 범위를 확인한 뒤에만 시작한다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:582,height:900}});
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=direct');
        const start=page.getByRole('button',{name:'새 소식 찾기',exact:true});
        await start.waitFor();
        await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='새 소식 찾기'&&!b.disabled));
        assert.equal(await page.getByText('한 번에 최대 10건',{exact:false}).count(),0);
        await start.click();
        await page.getByText('위 조건과 게시 기간으로 등록한 기관 게시판에서 새 소식을 찾을까요?',{exact:false}).waitFor();
        assert.deepEqual(await page.evaluate(()=>window.starts),[]);
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.starts.length===1);
        assert.deepEqual(await page.evaluate(()=>window.starts),[['default-news',1]]);
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=batch');
        await page.getByText('이전 실행에서 남겨 둔 자료가 있습니다.',{exact:false}).waitFor();
        await page.getByRole('button',{name:'이어서 진행',exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
    } finally {await browser.close();}
});

test('AI 한도·연결·일시 오류 대기는 사유를 알리고 보존된 작업을 재개한다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/news-workflow-release',{recursive:true});
    try {
        const page=await browser.newPage({viewport:{width:582,height:900}});
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        const cases={quota:'AI 사용 한도에 도달해 멈췄습니다.','legacy-local':'보관된 작업입니다. 이어서 진행하면 남은 자료를 처리합니다.',authentication:'AI 연결을 확인해야 합니다.',retry:'AI 응답을 받지 못해 재시도를 멈췄습니다.'};
        for(const [mode,message] of Object.entries(cases)) {
            await page.goto(`http://127.0.0.1:1420/tests/ui/news-collection.html?mode=${mode}`);
            await page.getByText(message,{exact:false}).waitFor();
            if(mode==='legacy-local')assert.doesNotMatch(await page.locator('main').innerText(),/20회|하루 AI 요청 상한|다음 집계일/);
            assert.equal(await page.getByRole('button',{name:'기존 작업을 이어서 진행하거나 종료해주세요',exact:true}).isDisabled(),true);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            if(mode==='quota') await page.screenshot({path:'../artifacts/news-workflow-release/quota-582.png',fullPage:true});
            await page.getByRole('button',{name:'이어서 진행',exact:true}).click();
            await page.waitForFunction(()=>window.resumes.length===1);
            assert.deepEqual(await page.evaluate(()=>window.resumes),[mode]);
            await page.getByText('최근 수집 · 완료',{exact:true}).waitFor();
            assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
            assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
        }
    } finally {await browser.close();}
});

test('복구 가능한 이전 실패 작업만 재개·종료하고 영구 실패에는 잘못된 재개를 제공하지 않는다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/news-workflow-release',{recursive:true});
    try {
        const page=await browser.newPage({viewport:{width:582,height:900}});
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=legacy-failure');
        await page.getByRole('button',{name:'문제 해결 후 이어서 진행',exact:true}).waitFor();
        await page.screenshot({path:'../artifacts/news-workflow-release/recoverable-failure-582.png',fullPage:true});
        await page.getByRole('button',{name:'문제 해결 후 이어서 진행',exact:true}).click();
        await page.waitForFunction(()=>window.resumes.length===1);
        assert.deepEqual(await page.evaluate(()=>window.resumes),['legacy-failure']);
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=legacy-failure');
        await page.getByRole('button',{name:'이 작업 종료',exact:true}).click();
        await page.waitForFunction(()=>window.cancels.length===1);
        assert.deepEqual(await page.evaluate(()=>window.cancels),['legacy-failure']);
        await page.getByText('최근 수집 · 사용자가 중지함',{exact:true}).waitFor();
        await page.getByRole('button',{name:'찾은 소식 확인',exact:true}).waitFor();
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=unrecoverable-waiting');
        await page.getByText('일부 자료의 분석 오류로 이어갈 수 없습니다.',{exact:false}).waitFor();
        assert.equal(await page.getByRole('button',{name:'기존 작업의 결과를 확인하고 종료해주세요',exact:true}).isDisabled(),true);
        assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
        await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).waitFor();
        await page.getByRole('button',{name:'이 작업 종료',exact:true}).click();
        await page.waitForFunction(()=>window.cancels.length===1);
        assert.deepEqual(await page.evaluate(()=>window.cancels),['unrecoverable-waiting']);
        await page.getByText('최근 수집 · 사용자가 중지함',{exact:true}).waitFor();
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=permanent-failure');
        await page.getByText('최근 수집 · 완료하지 못함',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'문제 해결 후 이어서 진행',exact:true}).count(),0);
        await page.getByRole('button',{name:'찾은 소식 확인',exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
        assert.deepEqual(errors,[]);
    } finally {await browser.close();}
});

test('이전 조건의 대기를 보존하면서 새 조건으로 시작하고 실행 중이거나 버전 미상인 작업은 보호한다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/news-workflow-release',{recursive:true});
    try {
        const page=await browser.newPage({viewport:{width:582,height:900}});
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=old-waiting');
        await page.getByText('수집 이력',{exact:true}).click();
        await page.getByText('상세 보기',{exact:true}).click();
        await page.getByText('이전 조건 · 버전 1 (현재 2). 이어서 진행해도 이전 조건을 사용합니다.',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:'../artifacts/news-workflow-release/previous-condition-582.png',fullPage:true});
        await page.getByRole('button',{name:'새 소식 찾기',exact:true}).click();
        await page.getByRole('button',{name:'확인하고 수집 시작',exact:true}).click();
        await page.waitForFunction(()=>window.starts.length===1);
        assert.deepEqual(await page.evaluate(()=>window.starts),[['default-news',2]]);
        await page.getByRole('button',{name:'이어서 진행',exact:true}).waitFor();
        await page.getByRole('button',{name:'이 작업 종료',exact:true}).waitFor();
        for(const mode of ['old-running','unknown-waiting']) {
            await page.goto(`http://127.0.0.1:1420/tests/ui/news-collection.html?mode=${mode}`);
            const blocked=page.getByRole('button',{name:mode==='old-running'?'수집한 정보를 분석하는 중…':'기존 작업을 이어서 진행하거나 종료해주세요',exact:true});
            await blocked.waitFor();
            assert.equal(await blocked.isDisabled(),true);
            assert.deepEqual(await page.evaluate(()=>window.starts),[]);
            assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
        }
    } finally {await browser.close();}
});

test('복지로 추가 페이지 확인은 완료로 오인하지 않고 일부 실패 후 기존 결과를 보존하며 재개한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:582,height:900}});
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=bokjiro-pages');
        await page.getByText('복지로: 추가 페이지 확인 중 · 원본 목록 120건 · 확인한 원본 30건 · 조건에 맞아 저장 12건',{exact:true}).waitFor();
        assert.equal(await page.getByText('완료',{exact:true}).count(),0);
        assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
        await page.getByRole('button',{name:'이 작업 중지',exact:true}).waitFor();
        await page.evaluate(()=>window.failBokjiroPage());await page.getByRole('button',{name:'상태 새로고침',exact:true}).click();
        await page.getByText('복지로: 일부 페이지 확인 실패 · 원본 목록 120건 · 확인한 원본 30건 · 조건에 맞아 저장 12건',{exact:true}).waitFor();
        await page.getByText('복지로의 일부 페이지를 확인하지 못해 멈췄습니다.',{exact:false}).waitFor();
        await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).waitFor();
        await page.getByRole('button',{name:'이 작업 종료',exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'기존 작업을 이어서 진행하거나 종료해주세요',exact:true}).isDisabled(),true);
        assert.deepEqual(await page.evaluate(()=>window.starts),[]);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.getByRole('button',{name:'이어서 진행',exact:true}).click();await page.getByText('최근 수집 · 완료',{exact:true}).waitFor();
        assert.deepEqual(await page.evaluate(()=>window.resumes),['bokjiro-pages']);assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
});

test('기관 수집의 일부 실패는 누적 저장 후보를 유지하고 재개 후에만 완료로 표시한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:582,height:900}});
        const errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto('http://127.0.0.1:1420/tests/ui/news-collection.html?mode=site-partial');
        await page.getByText('기관 사이트: 일부 자료 확인 실패 · 누적 저장 후보 135건',{exact:true}).waitFor();
        await page.getByText('수집을 시작하거나 계속할 수 없습니다.',{exact:false}).waitFor();
        assert.equal(await page.getByText('완료',{exact:true}).count(),0);
        await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).waitFor();await page.getByRole('button',{name:'이 작업 종료',exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'기존 작업을 이어서 진행하거나 종료해주세요',exact:true}).isDisabled(),true);
        await page.getByRole('button',{name:'이어서 진행',exact:true}).click();await page.getByText('최근 수집 · 완료',{exact:true}).waitFor();
        await page.getByText('수집 이력',{exact:true}).click();
        await page.getByText('상세 보기',{exact:true}).click();
        await page.getByText('기관 사이트: 확인 완료 · 누적 저장 후보 155건',{exact:true}).waitFor();
        assert.deepEqual(await page.evaluate(()=>window.resumes),['site-partial']);assert.deepEqual(await page.evaluate(()=>window.starts),[]);assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);assert.deepEqual(errors,[]);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    }finally{await browser.close();}
});

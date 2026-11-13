import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1422';
const artifactOutput=process.env.UI_TEST_ARTIFACTS||'../artifacts/news-empty-layout-20260918';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,mode,width=1280){
    const context=await browser.newContext({viewport:{width,height:950}}),page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-unified.html?mode='+mode);
    if(mode==='feed-error')await page.getByRole('alert').filter({hasText:'후보 목록을 불러오지 못했습니다.'}).waitFor();
    else await page.getByRole('heading',{name:/찾은 소식 \d+건/}).waitFor();
    return page;
}

test('첫 PC: 사이트 직접 등록과 기존 청록색 하단 실행 버튼, 빈 목록 도구 숨김',async t=>{
    const page=await fixture(t,'initial');
    const panel=page.locator('#news-collection');
    const site=panel.getByRole('link',{name:'수집 사이트 등록·관리',exact:true});
    assert.equal(await site.getAttribute('href'),'/sites');
    const start=panel.getByRole('button',{name:'새 소식 찾기',exact:true});
    assert.equal(await start.isDisabled(),true);
    assert.match(await start.getAttribute('class'),/bg-teal-700/);
    assert.ok((await start.boundingBox()).y>(await site.boundingBox()).y);
    assert.equal(await panel.getByRole('link',{name:'수집 범위·기간을 확인하고 저장',exact:true}).getAttribute('href'),'/settings?tab=keywords');
    assert.equal(await page.getByText(/영유아·아동 중심/).count(),0);
    assert.equal(await page.getByRole('group',{name:'소식 선택 및 저장'}).count(),0);
    assert.equal(await page.getByRole('navigation',{name:'소식 페이지',exact:true}).count(),0);
    assert.equal(await page.getByText('처리 기록',{exact:true}).count(),0);
    assert.equal(await page.getByRole('button',{name:'Notion 연결 확인',exact:true}).count(),0);
    assert.equal(await page.getByText(/위에서 새 소식을 찾아보세요/).count(),0);
    assert.ok((await page.evaluate(()=>globalThis.__calls.feed)).every(call=>call.view==='ready'));
});

test('0건이어도 실제 처리 기록은 유지하며 펼칠 때만 요청한다',async t=>{
    for(const mode of ['empty-records','unknown-counts','feed-error']){
        const page=await fixture(t,mode);
        assert.equal(await page.getByRole('group',{name:'소식 선택 및 저장'}).count(),0);
        assert.ok((await page.evaluate(()=>globalThis.__calls.feed)).every(call=>call.view==='ready'));
        await page.getByText('처리 기록',{exact:true}).click();
        await page.getByLabel('새 소식 처리 기록',{exact:true}).getByText('아직 분석을 시작하지 않은 자료입니다.',{exact:true}).waitFor();
    }
});

test('분석 실패 후 0건이면 이유를 표시하고 기존 청록색 실행 영역을 유지한다',async t=>{
    const page=await fixture(t,'legacy-partial');
    await page.getByRole('status').filter({hasText:'원문은 보존했으며 분석 1건을 마치지 못했습니다.'}).waitFor();
    assert.match(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).getAttribute('class'),/bg-teal-700/);
    assert.equal(await page.getByRole('group',{name:'소식 선택 및 저장'}).count(),0);
    assert.equal(await page.getByText('정보 수집 완료!',{exact:true}).count(),0);
});

test('3건: 원래 저장 기능 유지, 연결 확인은 접힌 도움말, 페이지 이동 없음',async t=>{
    const page=await fixture(t,'small');
    const toolbar=page.getByRole('group',{name:'소식 선택 및 저장'});
    await toolbar.waitFor();
    assert.equal(await page.getByRole('navigation',{name:'소식 페이지',exact:true}).count(),0);
    assert.equal(await page.getByRole('button',{name:'Notion 연결 확인',exact:true}).isVisible(),false);
    await page.getByText('Notion 연결 도움말',{exact:true}).click();
    await page.getByRole('button',{name:'Notion 연결 확인',exact:true}).click();
    await page.waitForFunction(()=>globalThis.__calls.verify.length===1);
    await page.getByRole('article').last().getByRole('checkbox').check();
    await toolbar.getByRole('button',{name:'소식 모음 파일 저장 (1)',exact:true}).click();
    await page.waitForFunction(()=>globalThis.__calls.html.length===1);
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.html),[[3]]);
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.upload),[]);
});

test('단일 페이지에서 여러 페이지로 전환해도 이동은 하단 한 곳, 0건 전환 시 도구 해제',async t=>{
    const page=await fixture(t,'small');
    await page.evaluate(()=>globalThis.__setRowCount(11));
    await page.getByRole('heading',{name:'찾은 소식 11건',exact:true}).waitFor();
    assert.equal(await page.getByRole('navigation',{name:'소식 페이지',exact:true}).count(),1);
    const nav=page.getByRole('navigation',{name:'소식 페이지',exact:true});
    assert.ok((await nav.boundingBox()).y>(await page.getByRole('article').last().boundingBox()).y);
    await nav.getByRole('button',{name:'다음 10건',exact:true}).click();
    await page.getByRole('article',{name:/소식 11 ·/}).waitFor();
    await page.evaluate(()=>globalThis.__setRowCount(0));
    await page.getByRole('heading',{name:'찾은 소식 0건',exact:true}).waitFor();
    await nav.waitFor({state:'hidden'});
    assert.equal(await page.getByRole('group',{name:'소식 선택 및 저장'}).count(),0);
});

test('Notion 미연결도 파일 저장 가능, 새로고침은 제목 옆 보조 버튼',async t=>{
    const page=await fixture(t,'disconnected');
    assert.equal(await page.getByRole('link',{name:'Notion 연결하기',exact:true}).count(),1);
    await page.getByRole('article').last().getByRole('checkbox').check();
    assert.equal(await page.getByRole('button',{name:'Notion에 등록 (1)',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'소식 모음 파일 저장 (1)',exact:true}).isEnabled(),true);
    const refresh=page.getByRole('button',{name:'목록 새로고침',exact:true});
    assert.equal(await refresh.textContent(),'');
    assert.equal(await refresh.getAttribute('title'),'목록 새로고침');
});

test('초기·기존 조건 화면 390/850/1280 폭에서 넘침 없이 유지',async t=>{
    await mkdir(artifactOutput,{recursive:true});
    for(const mode of ['initial','legacy-ready','small'])for(const width of [390,850,1280]){
        const page=await fixture(t,mode,width);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        const panel=page.locator('#news-collection');
        assert.match(await panel.getByRole('button',{name:'새 소식 찾기',exact:true}).getAttribute('class'),/bg-teal-700/);
        await page.screenshot({path:`${artifactOutput}/${mode}-${width}.png`,fullPage:true});
    }
});

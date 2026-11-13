import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1428';
const screenshots='../artifacts/news-detail-consistency-20260918';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});await mkdir(screenshots,{recursive:true});});
after(async()=>{await browser?.close();});
async function fixture(t,{width=1280,mode='normal'}={}){
    const context=await browser.newContext({viewport:{width,height:950},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{assert.deepEqual((await page.evaluate(()=>globalThis.__detailCalls)).blocked,[]);await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-detail-consistency.html?mode='+mode);
    await page.getByRole('button',{name:'새 소식 상세',exact:true}).waitFor({timeout:30000});
    return page;
}
const focused=locator=>locator.evaluate(node=>node===document.activeElement);
async function open(page,label){
    const trigger=page.getByRole('button',{name:label,exact:true});await trigger.click();
    const dialog=page.getByRole('dialog');await dialog.getByRole('heading',{name:'지원 내용',exact:true}).first().waitFor();
    return {dialog,trigger};
}

test('신규·기존 상세는 같은 폭·머리글·지원 내용 구성을 사용하고 원문은 노출하지 않음',async t=>{
    const page=await fixture(t);let firstWidth;
    for(const label of ['저장 소식 상세','새 소식 상세']){
        const {dialog}=await open(page,label);
        assert.equal(await dialog.getByRole('button',{name:'상세 정보 닫기',exact:true}).count(),1);
        assert.equal(await dialog.getByRole('link',{name:'원문 보기',exact:true}).count(),1);
        assert.equal(await dialog.getByText('문화/여가',{exact:true}).count(),1);
        assert.equal(await dialog.getByText('원문에서 확인한 내용입니다.',{exact:true}).count(),1);
        assert.equal(await dialog.locator('strong').filter({hasText:'함께 참여'}).count(),1);
        assert.doesNotMatch(await dialog.innerText(),/표시되면 안 되는 원문|COPYRIGHT|수집 원문 보기/);
        assert.equal(await dialog.locator('script').count(),0);
        assert.equal(await page.evaluate(()=>globalThis.__unsafe),undefined);
        const width=(await dialog.boundingBox()).width;
        if(firstWidth!==undefined)assert.equal(width,firstWidth);else firstWidth=width;
        await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    }
    const calls=await page.evaluate(()=>globalThis.__detailCalls);
    assert.deepEqual(calls,{blocked:[],saved:1,feed:1,similar:1});
});

test('빈 표시용 내용은 기존 ai_data 원문으로 되돌아가지 않음',async t=>{
    const page=await fixture(t,{mode:'empty'});
    for(const label of ['저장 소식 상세','새 소식 상세']){
        const {dialog}=await open(page,label);
        await dialog.getByText('읽을 수 있는 본문이 없습니다. 원문을 확인해주세요.',{exact:true}).waitFor();
        assert.doesNotMatch(await dialog.innerText(),/표시되면 안 되는 원문|COPYRIGHT/);
        await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    }
});

test('신규 상세도 키보드 포커스 가두기·Escape 복원·뒤 화면 스크롤 보호',async t=>{
    const page=await fixture(t);const {dialog,trigger}=await open(page,'새 소식 상세');
    const close=dialog.getByRole('button',{name:'상세 정보 닫기',exact:true});
    assert.equal(await focused(close),true);
    assert.equal(await page.evaluate(()=>document.body.style.overflow),'hidden');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await focused(dialog.getByRole('link',{name:'원문 보기',exact:true})),true);
    await page.keyboard.press('Tab');assert.equal(await focused(close),true);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    assert.equal(await focused(trigger),true);
    assert.equal(await page.evaluate(()=>document.body.style.overflow),'');
});

test('긴 본문은 가운데만 스크롤하고 닫기·원문 버튼이 계속 보임',async t=>{
    const page=await fixture(t,{mode:'long'});
    for(const label of ['저장 소식 상세','새 소식 상세']){
        const {dialog}=await open(page,label);
        const close=dialog.getByRole('button',{name:'상세 정보 닫기',exact:true});
        const source=dialog.getByRole('link',{name:'원문 보기',exact:true});
        const top=(await close.boundingBox()).y,bottom=(await source.boundingBox()).y;
        const body=dialog.locator('[data-news-detail-body]');
        assert.ok(await body.evaluate(node=>node.scrollHeight>node.clientHeight));
        await body.evaluate(node=>{node.scrollTop=node.scrollHeight;});
        assert.equal((await close.boundingBox()).y,top);
        assert.equal((await source.boundingBox()).y,bottom);
        await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    }
});

test('작은 화면에서도 가로 넘침 없이 기존 색상·크기를 유지',async t=>{
    for(const width of [375,1280]){
        const page=await fixture(t,{width});
        for(const [key,label] of [['saved','저장 소식 상세'],['new','새 소식 상세']]){
            const {dialog}=await open(page,label);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            assert.equal(await dialog.evaluate(node=>node.scrollWidth>node.clientWidth),false);
            await page.screenshot({path:`${screenshots}/${key}-${width}.png`});
            await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
        }
    }
});

test('상세 조회 실패 후 재시도는 상세 읽기만 다시 실행',async t=>{
    const page=await fixture(t,{mode:'error'});
    await page.getByRole('button',{name:'새 소식 상세',exact:true}).click();const dialog=page.getByRole('dialog');
    await dialog.getByRole('alert').waitFor();
    await dialog.getByRole('button',{name:'다시 불러오기',exact:true}).click();
    await page.waitForFunction(()=>globalThis.__detailCalls.feed===2);
    assert.deepEqual((await page.evaluate(()=>globalThis.__detailCalls)).blocked,[]);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
});

test('지원 금액·괄호·날짜는 불필요한 역슬래시 없이 보이고 원문 URL은 유지',async t=>{
    const page=await fixture(t,{mode:'boundary'});
    for(const label of ['저장 소식 상세','새 소식 상세']){
        const {dialog}=await open(page,label);
        await dialog.getByText('지원 내용: 1인당 5만원(총 50,000원)을 지원합니다.',{exact:true}).waitFor();
        await dialog.getByText('신청 기간: 2026-10-01부터 2026-10-31까지입니다.',{exact:true}).waitFor();
        assert.equal(await dialog.getByRole('heading',{name:'지원 내용',exact:true}).count(),1);
        assert.equal((await dialog.innerText()).includes('\\'),false);
        assert.equal(await dialog.getByRole('link',{name:'원문 보기',exact:true}).getAttribute('href'),'https://example.invalid/notice');
        assert.equal(await dialog.getByText('원문에서 확인한 내용입니다.',{exact:true}).count(),0);
        if(label==='새 소식 상세')await page.screenshot({path:`${screenshots}/boundary-1280.png`});
        await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    }
});

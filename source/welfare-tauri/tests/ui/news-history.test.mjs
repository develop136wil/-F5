import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1428';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,width=1280){
    const context=await browser.newContext({viewport:{width,height:950},reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{assert.deepEqual(await page.evaluate(()=>globalThis.__unexpected),[]);await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-history.html');
    await page.getByText('수집 이력',{exact:true}).waitFor({timeout:30000});
    return page;
}
test('완료 이력은 기본 접힘·짧은 행이며 상세·진행률은 펼칠 때만 확인',async t=>{
    const page=await fixture(t);
    assert.equal(await page.getByRole('progressbar').count(),0);
    assert.equal(await page.getByRole('button',{name:'목록에서 지우기',exact:true}).count(),0);
    await page.getByText('수집 이력',{exact:true}).click();
    await page.getByRole('button',{name:'목록에서 지우기',exact:true}).first().waitFor();
    assert.equal(await page.getByRole('button',{name:'목록에서 지우기',exact:true}).count(),5);
    assert.equal(await page.getByRole('progressbar').count(),0);
    await page.getByText('상세 보기',{exact:true}).first().click();
    await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).first().waitFor();
    assert.equal(await page.getByRole('progressbar').count(),0);
});
test('취소는 무변경·개별 정리와 전체 정리는 확인 후 실행',async t=>{
    const page=await fixture(t);await page.getByText('수집 이력',{exact:true}).click();
    page.once('dialog',dialog=>dialog.dismiss());await page.getByRole('button',{name:'완료 이력 정리',exact:true}).click();
    assert.equal((await page.evaluate(()=>globalThis.__clears)).length,0);
    page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'목록에서 지우기',exact:true}).first().click();
    await page.getByText('1개의 완료 이력을 목록에서 정리했습니다. 소식은 그대로 보존됩니다.',{exact:true}).waitFor();
    assert.equal((await page.evaluate(()=>globalThis.__clears))[0].ids.length,1);
    page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'완료 이력 정리',exact:true}).click();
    await page.getByText('4개의 완료 이력을 목록에서 정리했습니다. 소식은 그대로 보존됩니다.',{exact:true}).waitFor();
    assert.equal(await page.getByText('수집 이력',{exact:true}).count(),0);
});
test('현재 대기는 접힌 이력에 숨기지 않고 재개 버튼 유지',async t=>{
    const page=await fixture(t);await page.evaluate(()=>globalThis.__waiting());
    await page.getByRole('button',{name:'상태 새로고침',exact:true}).click();
    await page.getByRole('button',{name:'이어서 진행',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'목록에서 지우기',exact:true}).count(),0);
});
test('기존·통합 상세 모두 제목/목록 서식을 적용하고 HTML은 실행하지 않음',async t=>{
    const page=await fixture(t,582);
    for(const label of ['저장 소식 상세','새 소식 상세']){
        await page.getByRole('button',{name:label,exact:true}).click();const dialog=page.getByRole('dialog');
        await dialog.getByRole('heading',{name:'✅ 지원 내용',exact:true}).waitFor();
        assert.equal(await dialog.locator('strong').filter({hasText:'참여'}).count(),1);
        assert.ok((await dialog.getByRole('listitem').count())>=4);
        assert.equal((await dialog.innerText()).includes('###'),false);
        assert.equal(await page.evaluate(()=>globalThis.__unsafe),undefined);
        assert.equal(await dialog.locator('script').count(),0);
        await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    }
});
test('기존 밝은 스타일·모바일 폭 유지',async t=>{
    await mkdir('../artifacts/news-history-display-20260917',{recursive:true});
    for(const width of [425,1280]){
        const page=await fixture(t,width);await page.getByText('수집 이력',{exact:true}).click();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:`../artifacts/news-history-display-20260917/history-${width}.png`});
        await page.getByRole('button',{name:'저장 소식 상세',exact:true}).click();
        await page.getByRole('heading',{name:'✅ 지원 내용',exact:true}).waitFor();
        await page.screenshot({path:`../artifacts/news-history-display-20260917/detail-${width}.png`});
    }
});

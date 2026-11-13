import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1427';

test('기록보관 기본0일 유지·정리취소시무변경·명시확인후7일저장',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:850,height:700},serviceWorkers:'block'}),errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
        await page.goto(origin+'/tests/ui/news-retention.html');
        const input=page.getByRole('combobox',{name:'새 소식 기록 보관 기간',exact:true});
        await page.waitForFunction(()=>document.querySelector('select')?.disabled===false);
        assert.equal(await input.inputValue(),'0');
        assert.deepEqual(await page.evaluate(()=>globalThis.__retention.writes),[]);
        await input.selectOption('7');
        page.once('dialog',async dialog=>{assert.match(dialog.message(),/7일/);assert.match(dialog.message(),/되돌릴 수 없습니다/);await dialog.dismiss();});
        await page.getByRole('button',{name:'기록 보관 기간 저장',exact:true}).click();
        assert.deepEqual(await page.evaluate(()=>globalThis.__retention.writes),[]);
        page.once('dialog',async dialog=>{assert.match(dialog.message(),/찾은 소식, 저장한 자료, Notion 원본과 진행 중인 작업은 유지/);await dialog.accept();});
        await page.getByRole('button',{name:'기록 보관 기간 저장',exact:true}).click();
        await page.waitForFunction(()=>globalThis.__retention.writes.length===1);
        assert.deepEqual(await page.evaluate(()=>globalThis.__retention.writes),[7]);
        assert.deepEqual(await page.evaluate(()=>globalThis.__retention.unexpected),[]);
        assert.deepEqual(errors,[]);
        await mkdir('../artifacts/news-ui-polish-20260917',{recursive:true});
        await page.screenshot({path:'../artifacts/news-ui-polish-20260917/record-retention.png'});
    } finally {await browser.close();}
});

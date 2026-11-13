import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const model='gemini-3.5-flash-lite',embedding='gemini-embedding-001';
const confirmation='현재 연결한 키의 프로젝트에서 두 모델의 한도(분모)를 직접 확인했습니다.';
async function open(browser,mode='blank'){
    const page=await browser.newPage({viewport:{width:390,height:844}});
    page.external=[];page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();page.external.push(url.origin);return route.abort();});
    await page.goto(`${origin}/tests/ui/quota-observation.html?mode=${mode}`);
    await page.locator('summary').click();return page;
}
async function safe(page){assert.deepEqual(await page.evaluate(()=>window.unexpected),[]);assert.deepEqual(page.external,[]);assert.deepEqual(page.errors,[]);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}

test('관측값 없는 사용자는 빈 입력·미확인에서 시작하며 분모 확인 후에만 한 번 저장한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});await mkdir('../artifacts/quota-observation-20260909',{recursive:true});
    try{
        const page=await open(browser);
        for(const input of await page.getByRole('textbox').all())assert.equal(await input.inputValue(),'');
        assert.equal(await page.getByLabel(confirmation,{exact:true}).isChecked(),false);
        assert.equal(await page.getByRole('button',{name:'확인한 한도 저장',exact:true}).isDisabled(),true);
        await page.getByText('최근 28일 최대 사용량이나 사용한 횟수가 아닙니다.',{exact:false}).waitFor();
        await page.getByText('앱의 RPM 집계는 보호용 요청 단위이며, TPM은 입력량 추정치입니다.',{exact:false}).waitFor();
        assert.doesNotMatch(await page.locator('main').innerText(),/20회|0회 중지|7일이 지나면|앱 적용 상한/);
        for(const name of [model,embedding])for(const [label,value] of [['분당 요청 수 (RPM)','11'],['분당 토큰 수 (TPM)','12345'],['하루 요청 수 (RPD)','321']])await page.getByLabel(`${name} ${label}`,{exact:true}).fill(value);
        await page.getByLabel(confirmation,{exact:true}).check();
        await page.screenshot({path:'../artifacts/quota-observation-20260909/blank-filled-390.png',fullPage:true});
        await page.getByRole('button',{name:'확인한 한도 저장',exact:true}).evaluate(button=>{button.click();button.click();});
        await page.waitForFunction(()=>window.calls.length===1);
        assert.equal(await page.getByLabel(confirmation,{exact:true}).isChecked(),false);
        assert.equal(await page.getByLabel(`${model} 분당 요청 수 (RPM)`,{exact:true}).isDisabled(),true);
        assert.equal(await page.evaluate(()=>window.drafts.dirty('quotaObservationDraft')),true);
        await page.evaluate(()=>window.finishQuota());
        await page.getByText('확인한 프로젝트 한도를 저장했습니다.',{exact:false}).waitFor();
        assert.equal(await page.evaluate(()=>window.drafts.dirty('quotaObservationDraft')),false);
        assert.deepEqual(await page.evaluate(()=>window.calls),[{confirmed:true,models:{[model]:{rpm:11,tpm:12345,rpd:321},[embedding]:{rpm:11,tpm:12345,rpd:321}}}]);
        await safe(page);
    }finally{await browser.close();}
});

test('재확인·실패·화면 이동에서 초안을 보존하고 오래된 한도나 빈 키는 확정하지 않는다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await open(browser,'current');
        const effective=page.getByText('분당 요청 보호:',{exact:false});
        assert.equal(await effective.count(),2);
        for(const entry of await effective.all())assert.doesNotMatch(await entry.innerText(),/RPD/);
        await page.getByLabel(`${model} 분당 요청 수 (RPM)`,{exact:true}).fill('17');
        await page.evaluate(()=>window.refreshObservation());
        assert.equal(await page.getByLabel(`${model} 분당 요청 수 (RPM)`,{exact:true}).inputValue(),'17');
        await page.getByLabel(confirmation,{exact:true}).check();await page.getByRole('button',{name:'확인한 한도 저장',exact:true}).click();
        await page.waitForFunction(()=>window.calls.length===1);await page.evaluate(()=>window.failQuota());
        await page.getByRole('alert').waitFor();assert.equal(await page.getByLabel(confirmation,{exact:true}).isChecked(),false);
        assert.equal(await page.getByLabel(`${model} 분당 요청 수 (RPM)`,{exact:true}).inputValue(),'17');
        await page.getByRole('button',{name:'시험 화면 이동',exact:true}).click();await page.getByRole('button',{name:'시험 화면 이동',exact:true}).click();await page.locator('summary').click();
        assert.equal(await page.getByLabel(`${model} 분당 요청 수 (RPM)`,{exact:true}).inputValue(),'17');
        assert.equal(await page.evaluate(()=>window.drafts.dirty('quotaObservationDraft')),true);await safe(page);await page.close();
        for(const mode of ['expired','no-key']){
            const next=await open(browser,mode);
            for(const input of await next.getByRole('textbox').all())assert.equal(await input.inputValue(),'');
            assert.equal(await next.getByRole('button',{name:'확인한 한도 저장',exact:true}).isDisabled(),true);
            if(mode==='no-key')assert.equal(await next.getByLabel(confirmation,{exact:true}).isDisabled(),true);
            await safe(next);await next.close();
        }
    }finally{await browser.close();}
});

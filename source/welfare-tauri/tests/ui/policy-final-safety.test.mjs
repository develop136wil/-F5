import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const artifactOutput=process.env.UI_TEST_ARTIFACTS||'../artifacts/policy-final-fixes-20260917';
async function pageFor(t,path) {
    const browser=await chromium.launch({channel:'msedge',headless:true});t.after(()=>browser.close());
    const page=await browser.newPage({viewport:{width:1280,height:900},serviceWorkers:'block'});
    page.setDefaultTimeout(10000);
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===origin&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
    t.after(()=>assert.deepEqual(errors,[]));await page.goto(origin+path);return page;
}

test('구제안·원문 보류·미확인 상태는 같은 카드에서 설명하며 바로 적용을 제공하지 않는다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-review.html?mode=stale');
    for(const mode of ['stale','source-review','unknown-state']) {
        await page.goto(`${origin}/tests/ui/policy-review.html?mode=${mode}`);
        const card=page.locator('#policy-review-1');
        await card.locator('button[aria-controls="policy-detail-1"]').click();
        await card.getByText('원문이 길어 분석을 완료하지 못했습니다.',{exact:false}).waitFor();
        await card.getByRole('heading',{name:'이전에 저장된 비교 내용',exact:true}).waitFor();
        assert.equal(await card.getByRole('button',{name:'바로 적용',exact:true}).count(),0);
        assert.equal(await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).count(),0);
        assert.equal(await card.getByRole('heading',{name:'현재 Notion 내용',exact:true}).count(),0);
        assert.equal(await page.getByRole('region',{name:'저장된 정책 보류 기록'}).count(),0);
        assert.ok(await page.evaluate(()=>window.reviewExclusions.some(ids=>ids.includes(1))));
        assert.equal(await page.evaluate(()=>window.individualCalls.length+window.policyCalls.length),0);
    }
    await mkdir(artifactOutput,{recursive:true});
    await page.screenshot({path:`${artifactOutput}/stale-inline-review.png`});
});

test('읽기 실패와 구형 응답을 변경 0건 성공으로 표시하지 않는다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-watch.html?mode=seed');
    await page.getByRole('button',{name:'지금 점검',exact:true}).waitFor();
    await page.evaluate(async()=>{const{welfareApi}=await import('/src/api/welfare.js');welfareApi.runPolicyWatchCheck=async()=>({checked:2,changed:0,failed:2,status:'error'});});
    await page.getByRole('button',{name:'지금 점검',exact:true}).click();
    await page.getByText(/점검 실패 — 확인 성공 0건.*읽기 실패 2건/).waitFor();
    assert.equal(await page.getByText(/^점검 완료 —/).count(),0);
    await page.evaluate(async()=>{const{welfareApi}=await import('/src/api/welfare.js');welfareApi.runPolicyWatchCheck=async()=>({checked:2,changed:0});});
    await page.getByRole('button',{name:'지금 점검',exact:true}).click();
    await page.getByText(/점검 결과를 확인하지 못했습니다/).waitFor();
});

test('재점검 뒤 같은 속성의 새 제안과 이전 반영 취소를 별도로 보존한다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-review.html?mode=history');
    const card=page.locator('#policy-review-1');
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    await card.getByText('새로운 제안',{exact:true}).waitFor();
    await card.locator('[data-policy-field-group="applied"] summary').click();
    await card.getByRole('button',{name:'적용 취소',exact:true}).waitFor();
    assert.equal(await card.getByRole('button',{name:'바로 적용',exact:true}).count(),1);
    assert.equal(await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).count(),1);
    assert.equal(await page.evaluate(()=>window.individualCalls.length+window.policyCalls.length),0);
    await page.goto(`${origin}/tests/ui/policy-review.html?mode=history-only`);
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    await card.locator('[data-policy-field-group="applied"] summary').click();
    await card.getByText('반영 상태 확인 필요',{exact:true}).waitFor();
    assert.equal(await card.getByRole('button',{name:'적용 취소',exact:true}).count(),1);
    assert.equal(await card.getByRole('button',{name:'바로 적용',exact:true}).count(),0);
    assert.equal(await card.getByRole('button',{name:'완료한 카드 정리',exact:true}).count(),0);
    await page.goto(`${origin}/tests/ui/policy-review.html?mode=history-uncertain`);
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    await card.locator('[data-policy-field-group="applied"] summary').click();
    assert.equal(await card.getByRole('button',{name:'적용 취소',exact:true}).isDisabled(),true);
    await card.getByText('불확실한 요청은 다시 보내지 않습니다.',{exact:false}).waitFor();
});

test('로컬 저장 실패가 있으면 뒤의 개별 원문 확인 항목도 추가 반영하지 않는다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-review.html?mode=local-save-bulk');
    page.on('dialog',dialog=>dialog.accept());
    const card=page.locator('#policy-review-1');
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    for(let i=0;i<2;i++)await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).first().click();
    await page.getByRole('region',{name:'선택한 정책 작업'}).getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).click();
    await page.getByText(/추가 반영 중단 — Notion 확인 필요/).first().waitFor();
    assert.equal(await page.evaluate(()=>window.policyCalls.length),1);
    assert.equal(await page.evaluate(()=>window.individualCalls.length),0);
});

test('실패한 작업이 종료 상태로 복원되면 재진입 뒤 점검 버튼이 잠기지 않는다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-hold-resume.html?mode=clean');
    await page.getByRole('link',{name:'다른 화면',exact:true}).click();
    await page.evaluate(async()=>{
        const{welfareApi}=await import('/src/api/welfare.js');
        welfareApi.getPolicyStatus=async()=>({running:false,error:'browser startup failed',message:'점검 실패'});
        welfareApi.getPolicyFullCheckStatus=async()=>({running:false,error:'browser startup failed',message:'통합 점검 실패'});
    });
    await page.getByRole('link',{name:'정책 화면',exact:true}).click();
    const main=page.getByRole('button',{name:'정책 정보 최신인지 확인하기',exact:true});
    await main.waitFor();assert.equal(await main.isEnabled(),true);
    await page.getByRole('button',{name:'세부 작업',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:/^Notion 정책 불러오기/}).isEnabled(),true);
    assert.equal(await page.getByRole('button',{name:/^저장된 변경 제안 다시 점검/}).isEnabled(),true);
});

test('개별 반영의 로컬 저장 실패 직후 복구 기록을 다시 읽어 보여준다',async t=>{
    const page=await pageFor(t,'/tests/ui/policy-review.html?mode=local-save-individual');
    const card=page.locator('#policy-review-1');
    await card.locator('button[aria-controls="policy-detail-1"]').click();
    await card.getByRole('button',{name:'바로 적용',exact:true}).click();
    await card.getByText('반영 확인 필요',{exact:true}).waitFor();
    await card.getByText('반영·복구 기록 1개',{exact:true}).click();
    await card.getByRole('button',{name:'적용 취소',exact:true}).waitFor();
    assert.equal(await card.getByRole('button',{name:'바로 적용',exact:true}).count(),0);
    assert.equal(await page.evaluate(()=>window.individualCalls.length),1);
});

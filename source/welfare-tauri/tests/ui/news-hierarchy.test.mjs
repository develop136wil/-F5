import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
async function fixture(t,mode='',width=850){
    const browser=await chromium.launch({channel:'msedge',headless:true});t.after(()=>browser.close());
    const page=await browser.newPage({viewport:{width,height:900}});
    page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    await page.goto(`${origin}/tests/ui/news-hierarchy.html?mode=${mode}`);return page;
}
const button=(scope,name)=>scope.getByRole('button',{name,exact:true});
const items=page=>page.locator('[data-news-item]');

test('새 소식의 제목 위계·고정 선택열·긴 제목·보조 안내 순서를 좁은 창과 다크모드에서 유지한다',async t=>{
    const page=await fixture(t);await mkdir('../artifacts/news-hierarchy',{recursive:true});
    for(const mode of ['', 'profile']){
        await page.goto(`${origin}/tests/ui/news-hierarchy.html?mode=${mode}`);
        await page.getByRole('heading',{name:'찾은 소식 3건',exact:true}).waitFor();await page.getByRole('heading',{name:'수집 범위',exact:true}).waitFor();
        for(const width of [582,850,1280])for(const dark of [false,true]){
            await page.setViewportSize({width,height:950});await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            const hierarchy=await page.evaluate(()=>{const nodes=[document.querySelector('h1'),document.getElementById('news-results-title'),[...document.querySelectorAll('h2')].find(node=>node.textContent==='수집 범위')];return nodes.map(node=>({size:parseFloat(getComputedStyle(node).fontSize),weight:getComputedStyle(node).fontWeight}));});
            assert.deepEqual(hierarchy,[{size:28,weight:'700'},{size:20,weight:'600'},{size:16,weight:'600'}]);
            const results=page.getByRole('region',{name:'찾은 소식 3건',exact:true});assert.equal(await results.getByRole('group',{name:'소식 선택 및 저장'}).count(),1);assert.equal(await results.locator('[data-news-item]').count(),3);
            const metrics=await items(page).evaluateAll(rows=>rows.map(row=>{const check=row.querySelector('input'),body=check.nextElementSibling,dismiss=row.lastElementChild,title=row.querySelector('[data-news-title]'),meta=row.querySelector('[data-news-meta]'),review=row.querySelector('details');const rect=node=>node.getBoundingClientRect();return {checkWidth:rect(check).width,dismissWidth:rect(dismiss).width,dismissX:rect(dismiss).x,firstLine:rect(title).top+12,checkCenter:rect(check).top+rect(check).height/2,dismissCenter:rect(dismiss).top+rect(dismiss).height/2,bodyWidth:rect(body).width,overflow:row.scrollWidth>row.clientWidth,metaAfterTitle:rect(meta).top>=rect(title).bottom,reviewAfterMeta:!review||rect(review).top>=rect(meta).bottom,reviewClosed:!review||!review.open};}));
            for(const metric of metrics){assert.equal(metric.checkWidth,16);assert.equal(metric.dismissWidth,36);assert.ok(Math.abs(metric.checkCenter-metric.firstLine)<1);assert.ok(Math.abs(metric.dismissCenter-metric.firstLine)<1);assert.ok(metric.bodyWidth>200);assert.equal(metric.overflow,false);assert.equal(metric.metaAfterTitle,true);assert.equal(metric.reviewAfterMeta,true);assert.equal(metric.reviewClosed,true);}
            assert.ok(metrics.every(metric=>Math.abs(metric.dismissX-metrics[0].dismissX)<1));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await page.screenshot({path:`../artifacts/news-hierarchy/${mode||'legacy'}-${dark?'dark':'light'}-${width}.png`,fullPage:true,animations:'disabled'});
        }
    }
    assert.deepEqual(page.errors,[]);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.unexpected),[]);
});

test('소식 선택·미리보기·접힌 원문 안내·제외 확인은 서로 섞이지 않고 키보드로 조작된다',async t=>{
    const page=await fixture(t);const item=items(page).first();await item.waitFor();
    const checkbox=item.getByRole('checkbox');await checkbox.focus();await page.keyboard.press('Space');assert.equal(await checkbox.isChecked(),true);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.detail),[]);
    const preview=item.getByRole('button',{name:/전체 내용 미리보기$/});await preview.focus();await page.keyboard.press('Enter');const detail=page.getByRole('dialog');await detail.waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.detail),[1]);
    await page.keyboard.press('Escape');await detail.waitFor({state:'detached'});assert.equal(await preview.evaluate(el=>el===document.activeElement),true);
    const summary=item.locator('summary');await summary.focus();await page.keyboard.press('Enter');assert.equal(await item.locator('details').getAttribute('open'),'');await item.getByText('첨부 안내문을 직접 확인해주세요.',{exact:true}).waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.detail),[1]);
    await page.keyboard.press('Enter');assert.equal(await item.locator('details').getAttribute('open'),null);
    const dismiss=item.getByRole('button',{name:/후보에서 제외$/});await dismiss.focus();await page.keyboard.press('Enter');const confirm=page.getByRole('dialog',{name:'1건을 후보에서 제외할까요?',exact:true});await confirm.waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.dismiss),[]);
    await page.keyboard.press('Escape');await confirm.waitFor({state:'detached'});assert.equal(await checkbox.isChecked(),true);assert.equal(await dismiss.evaluate(el=>el===document.activeElement),true);
    assert.deepEqual(page.errors,[]);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.unexpected),[]);
});

test('원문 확인 안내를 접어도 파일·Notion 저장과 불확실 등록 재시도의 확인 절차를 유지한다',async t=>{
    const page=await fixture(t);const item=items(page).first();await item.waitFor();await item.getByRole('checkbox').check();
    await button(page,'Notion에 등록 (1)').click();let confirm=page.getByRole('dialog',{name:'Notion에 등록하기 전에 확인해주세요',exact:true});await confirm.waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.upload),[]);
    await button(confirm,'취소').click();assert.equal(await item.getByRole('checkbox').isChecked(),true);
    await button(page,'소식 모음 파일 저장 (1)').click();confirm=page.getByRole('dialog',{name:'소식 모음 파일로 저장하기 전에 확인해주세요',exact:true});await confirm.waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.html),[]);
    await button(confirm,'확인했어요 · 파일 저장').click();await page.waitForFunction(()=>newsFixture.calls.html.length===1);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.html),[[1]]);
    await button(page,'Notion에 등록 (1)').click();confirm=page.getByRole('dialog',{name:'Notion에 등록하기 전에 확인해주세요',exact:true});await button(confirm,'확인했어요 · Notion 등록').click();await page.waitForFunction(()=>newsFixture.calls.upload.length===1);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.upload),[[1]]);
    await items(page).nth(1).getByRole('checkbox').check();await button(page,'Notion 확인 후 재시도 허용').click();confirm=page.getByRole('dialog',{name:'Notion에서 등록 결과를 확인하셨나요?',exact:true});await confirm.waitFor();assert.deepEqual(await page.evaluate(()=>newsFixture.calls.retry),[]);
    await button(confirm,'미등록 확인 · 재시도 허용').click();await page.waitForFunction(()=>newsFixture.calls.retry.length===1);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.retry),[[2]]);assert.equal(await page.evaluate(()=>newsFixture.calls.upload.length),1,'재시도 허용은 자동 등록하지 않음');
    assert.deepEqual(page.errors,[]);assert.deepEqual(await page.evaluate(()=>newsFixture.calls.unexpected),[]);
});

test('소식 수는 확실한 목록에서만 표시하고 로딩·오류를 0건으로 오해하게 하지 않는다',async t=>{
    const page=await fixture(t,'pending');await page.getByText('소식 목록을 불러오고 있습니다.',{exact:true}).waitFor();await page.getByRole('heading',{name:'찾은 소식',exact:true}).waitFor();assert.equal(await page.getByRole('heading',{name:/찾은 소식 \d+건/}).count(),0);
    await page.evaluate(()=>window.finishNewsList());await page.getByRole('heading',{name:'찾은 소식 3건',exact:true}).waitFor();
    await page.evaluate(()=>window.newsPhase('error'));await page.getByRole('alert').filter({hasText:'후보 목록을 불러오지 못했습니다.'}).waitFor();await page.getByRole('heading',{name:'찾은 소식',exact:true}).waitFor();assert.equal(await page.getByRole('heading',{name:/찾은 소식 \d+건/}).count(),0);
    await page.evaluate(()=>window.newsPhase('ready'));await page.getByRole('heading',{name:'찾은 소식 3건',exact:true}).waitFor();
    await page.evaluate(()=>window.newsPhase('empty'));await page.getByRole('heading',{name:'찾은 소식 0건',exact:true}).waitFor();assert.equal(await items(page).count(),0);assert.deepEqual(page.errors,[]);
});

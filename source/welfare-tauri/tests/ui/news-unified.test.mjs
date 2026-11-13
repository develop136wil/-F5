import {before,after,test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1422';
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{await browser?.close();});
async function fixture(t,width=1280){
    const context=await browser.newContext({viewport:{width,height:950}}),page=await context.newPage(),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
    await page.goto(origin+'/tests/ui/news-unified.html');
    await page.getByRole('heading',{name:'찾은 소식 105건',exact:true}).waitFor({timeout:30000});
    return page;
}
const article=(page,n)=>page.getByRole('article',{name:`소식 ${n} · 아동 교육·돌봄 프로그램 참여 안내 소식`,exact:true});
const waitCalls=(page,key)=>page.waitForFunction(key=>globalThis.__calls[key].length>0,key);

test('105건도10개카드: 단일목록·페이지선택해제·제목상세와기간표시',async t=>{
    const page=await fixture(t);
    assert.equal(await page.getByRole('article').count(),10);
    assert.equal(await page.getByRole('button',{name:/새 소식 후보로 가져오기/}).count(),0);
    assert.equal(await page.getByText('이번 수집 결과',{exact:true}).count(),0);
    await page.getByText('최근 30일 · 게시일 기준',{exact:true}).waitFor();
    await page.getByText('수집 이력',{exact:true}).click();
    await page.getByText('상세 보기',{exact:true}).click();
    await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).click();
    await page.waitForFunction(()=>document.activeElement.id==='news-results-title');
    assert.equal(await article(page,3).getByText(/게시일: 확인 필요/).count(),0);
    assert.equal(await article(page,1).getByText('도봉구 거주 아동과 보호자',{exact:true}).count(),0);
    assert.equal(await article(page,1).getByText('교육·돌봄 프로그램 참여를 지원합니다.',{exact:true}).count(),0);
    await article(page,1).getByRole('button',{name:/전체 내용 미리보기/}).click();
    const dialog=page.getByRole('dialog');await dialog.getByText('교육비와 돌봄 프로그램을 지원합니다.',{exact:false}).waitFor();
    assert.equal((await page.evaluate(()=>globalThis.__calls.prepare)).length,0);
    assert.equal((await page.evaluate(()=>globalThis.__calls.details)).length,1);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    await article(page,1).getByRole('checkbox').check();
    await page.getByRole('button',{name:'다음 10건',exact:true}).first().click();
    await article(page,11).waitFor();
    assert.equal(await page.getByRole('checkbox',{checked:true}).count(),0);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'news-results-title');
});

test('선택즉시최종등록: prepare1회+upload1회, 중간가져오기없음',async t=>{
    const page=await fixture(t);
    await article(page,3).getByRole('checkbox').check();
    await page.getByRole('button',{name:'Notion에 등록 (1)',exact:true}).click();
    await waitCalls(page,'upload');
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.upload),[[3]]);
    assert.equal((await page.evaluate(()=>globalThis.__calls.prepare)).length,1);
});

test('근거부족행만확인: 취소시prepare없고 혼합재시도는불확실항목만',async t=>{
    const page=await fixture(t);
    await article(page,1).getByRole('checkbox').check();
    await page.getByRole('button',{name:'Notion에 등록 (1)',exact:true}).click();
    await page.getByRole('dialog').getByRole('button',{name:'취소',exact:true}).click();
    assert.equal((await page.evaluate(()=>globalThis.__calls.prepare)).length,0);
    await article(page,1).getByRole('checkbox').uncheck();
    await article(page,2).getByRole('checkbox').check();await article(page,3).getByRole('checkbox').check();
    await page.getByRole('button',{name:'Notion 확인 후 재시도 허용',exact:true}).click();
    await page.getByRole('dialog').getByRole('button').last().click();await waitCalls(page,'retry');
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.retry),[[2]]);
});

test('prepare중화면이탈은후속외부등록을시작하지않음',async t=>{
    const page=await fixture(t);await page.evaluate(()=>globalThis.__holdPrepare());
    await article(page,3).getByRole('checkbox').check();await page.getByRole('button',{name:'Notion에 등록 (1)',exact:true}).click();
    await waitCalls(page,'prepare');
    await page.evaluate(async()=>{globalThis.__leave();await new Promise(resolve=>requestAnimationFrame(resolve));globalThis.__finishPrepare();});
    await page.getByRole('heading',{name:'새 소식',exact:true}).waitFor({state:'hidden'});
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.upload),[]);
});

test('처리기록은기본접힘·별도조회, 후보목록은유지하고 기간직접지정은검증후저장',async t=>{
    const page=await fixture(t);
    assert.equal(await page.getByRole('button',{name:'확인 필요',exact:true}).count(),0);
    assert.equal(await page.getByRole('button',{name:'분석 대기·실패',exact:true}).count(),0);
    assert.ok((await page.evaluate(()=>globalThis.__calls.feed)).every(call=>call.view==='ready'));
    await article(page,1).getByRole('checkbox').check();
    await page.getByText('처리 기록',{exact:true}).click();
    const records=page.getByLabel('새 소식 처리 기록',{exact:true});
    await records.getByText('아직 분석을 시작하지 않은 자료입니다.',{exact:true}).waitFor();
    assert.equal(await records.getByRole('checkbox').count(),0);
    await records.getByRole('button',{name:'추가 확인',exact:true}).click();
    await records.getByRole('button',{name:'소식 201 · 아동 교육·돌봄 프로그램 참여 안내',exact:true}).click();
    await page.getByRole('dialog').waitFor();
    assert.deepEqual(await page.evaluate(()=>globalThis.__calls.details),[]);
    assert.equal(await page.getByRole('dialog').getByRole('alert').count(),0);
    await page.keyboard.press('Escape');
    await records.getByRole('button',{name:'제외·종료',exact:true}).click();
    await records.getByText('이 분류에 해당하는 기록이 없습니다.',{exact:true}).waitFor();
    await page.getByText('처리 기록',{exact:true}).click();
    assert.equal(await records.count(),0);
    assert.equal(await article(page,1).getByRole('checkbox').isChecked(),true);
    assert.equal(await page.getByRole('article').count(),10);
    await page.getByRole('heading',{name:'찾은 소식 105건',exact:true}).waitFor();
    await page.getByLabel('수집 기간',{exact:true}).selectOption('custom');
    await page.getByRole('button',{name:'기간 저장 시험'}).click();
    assert.match(await page.getByLabel('기간 저장 결과').textContent(),/올바르게/);
    await page.getByLabel('게시일 시작').fill('2026-09-01');await page.getByLabel('게시일 종료').fill('2026-09-17');
    await page.getByRole('button',{name:'기간 저장 시험'}).click();
    assert.equal(JSON.parse(await page.getByLabel('기간 저장 결과').textContent()).publication_start_date,'2026-09-01');
});

test('기존밝은카드레이아웃 582/850/1280 폭에서가로넘침없음',async t=>{
    await mkdir('../artifacts/news-simplification-20260917',{recursive:true});
    for(const width of [582,850,1280]){
        const page=await fixture(t,width);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.equal(await page.getByRole('article').first().evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
        await page.screenshot({path:`../artifacts/news-simplification-20260917/news-unified-${width}.png`});
        await page.getByRole('heading',{name:'찾은 소식 105건',exact:true}).scrollIntoViewIfNeeded();
        await page.evaluate(()=>document.getElementById('news-results-title').scrollIntoView({block:'start'}));
        await page.screenshot({path:`../artifacts/news-simplification-20260917/news-unified-list-${width}.png`});
    }
});

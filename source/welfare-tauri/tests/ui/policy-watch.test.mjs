import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
async function fixture(t,mode=''){
    const browser=await chromium.launch({channel:'msedge',headless:true});t.after(()=>browser.close());
    const page=await browser.newPage({viewport:{width:850,height:850}});
    page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    await page.goto(`${origin}/tests/ui/policy-watch.html?mode=${mode}`);
    await page.getByRole('heading',{name:'웹페이지 변경 확인',exact:true}).waitFor();return page;
}
const button=(scope,name)=>scope.getByRole('button',{name,exact:true});
const row=(page,name)=>page.getByRole('row').filter({hasText:name});

test('직접 감시 추가·점검·변경 비교·비활성·삭제는 각 명시적 조작으로만 실행된다',async t=>{
    const page=await fixture(t);
    await page.getByText('확인할 웹페이지가 없습니다. 위에서 주소를 추가하세요.',{exact:true}).waitFor();
    await button(page,'추가').click();await page.getByText('URL을 입력하세요',{exact:true}).waitFor();assert.equal(await page.evaluate(()=>watchFixture.calls.add.length),0);
    await page.getByLabel('이름',{exact:true}).fill('시험 양육 지원');await page.getByLabel('정책 페이지 URL',{exact:true}).fill('https://example.org/welfare');await page.getByLabel('범위(태그)',{exact:true}).fill('영유아');
    await button(page,'추가').click();const item=row(page,'시험 양육 지원');await item.waitFor();
    assert.deepEqual(await page.evaluate(()=>watchFixture.calls.add),[{name:'시험 양육 지원',url:'https://example.org/welfare',scope:'영유아'}]);assert.equal(await page.evaluate(()=>watchFixture.calls.check),0);assert.deepEqual(await page.evaluate(()=>watchFixture.calls.diff),[]);
    const source=item.locator('a');assert.equal(await source.getAttribute('href'),'https://example.org/welfare');assert.match(await source.getAttribute('rel'),/noopener/);
    await button(page,'지금 점검').click();await page.getByText('점검 완료 — 1건 중 변경 1건',{exact:true}).waitFor();
    await button(item,'변경 내용 보기').click();await page.getByRole('dialog',{name:'시험 양육 지원 — 변경 내용',exact:true}).waitFor();
    await page.getByText('지원 인원 10명',{exact:true}).waitFor();await page.getByText('지원 인원 20명',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'원본 페이지 열기 ↗',exact:true}).getAttribute('href'),'https://example.org/welfare');
    await button(page,'변경 내용 닫기').click();
    await item.locator('[role=switch][aria-checked=true]').click();await item.locator('[role=switch][aria-checked=false]').waitFor();
    assert.equal(await item.evaluate(el=>getComputedStyle(el).opacity),'0.5');
    await button(page,'지금 점검').click();await page.getByText('점검 완료 — 0건 중 변경 0건',{exact:true}).waitFor();
    page.once('dialog',dialog=>dialog.dismiss());await button(item,'삭제').click();assert.deepEqual(await page.evaluate(()=>watchFixture.calls.delete),[]);
    page.once('dialog',dialog=>dialog.accept());await button(item,'삭제').click();await page.getByText('확인할 웹페이지가 없습니다. 위에서 주소를 추가하세요.',{exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(()=>watchFixture.calls.toggle),[{id:3,enabled:false}]);assert.deepEqual(await page.evaluate(()=>watchFixture.calls.delete),[3]);assert.deepEqual(await page.evaluate(()=>watchFixture.unexpected),[]);assert.deepEqual(page.errors,[]);
});

test('직접 감시 조회·추가·점검·변경 비교·토글·삭제 오류는 결과를 성공으로 표시하지 않고 재시도한다',async t=>{
    const page=await fixture(t,'seed');
    await row(page,'시험 정책 1').waitFor();
    await page.evaluate(()=>{watchFixture.failures.list={message:'시험 목록 조회 실패'};});
    await button(page,'지금 점검').click();await page.getByRole('alert').filter({hasText:'웹페이지 목록을 불러오지 못했습니다.'}).waitFor();
    await page.evaluate(()=>{delete watchFixture.failures.list;});await button(page,'다시 불러오기').click();await row(page,'시험 정책 1').waitFor();
    await page.getByLabel('이름',{exact:true}).fill('실패 후 재시도');await page.getByLabel('정책 페이지 URL',{exact:true}).fill('https://example.org/retry');
    await page.evaluate(()=>{watchFixture.failures.add={message:'시험 추가 실패'};});await button(page,'추가').click();await page.getByText('추가 실패: 시험 추가 실패',{exact:true}).waitFor();assert.equal(await page.getByLabel('정책 페이지 URL',{exact:true}).inputValue(),'https://example.org/retry');
    await page.evaluate(()=>{delete watchFixture.failures.add;});await button(page,'추가').click();await row(page,'실패 후 재시도').waitFor();
    await page.evaluate(()=>{watchFixture.failures.check={response:{status:409}};});await button(page,'지금 점검').click();await page.getByText('점검 실패: 이미 점검이 진행 중입니다',{exact:true}).waitFor();
    await page.evaluate(()=>{delete watchFixture.failures.check;});await button(page,'지금 점검').click();await page.getByText('점검 완료 — 3건 중 변경 3건',{exact:true}).waitFor();
    const item=row(page,'시험 정책 1');
    await page.evaluate(()=>{watchFixture.failures['diff-1']={message:'시험 비교 실패'};});await button(item,'변경 내용 보기').click();await page.getByText('변경 내용을 불러오지 못했습니다',{exact:true}).waitFor();assert.equal(await page.getByRole('dialog').count(),0);
    await page.evaluate(()=>{delete watchFixture.failures['diff-1'];});await button(item,'변경 내용 보기').click();await button(page,'변경 내용 닫기').click();
    await page.evaluate(()=>{watchFixture.failures.toggle={message:'시험 토글 실패'};});await item.locator('[role=switch][aria-checked=true]').click();await page.getByText('변경 실패',{exact:true}).waitFor();await item.locator('[role=switch][aria-checked=true]').waitFor();
    await page.evaluate(()=>{delete watchFixture.failures.toggle;});await item.locator('[role=switch][aria-checked=true]').click();await item.locator('[role=switch][aria-checked=false]').waitFor();
    await page.evaluate(()=>{watchFixture.failures.delete={message:'시험 삭제 실패'};});page.once('dialog',dialog=>dialog.accept());await button(item,'삭제').click();await page.getByText('삭제 실패',{exact:true}).waitFor();await item.waitFor();
    await page.evaluate(()=>{delete watchFixture.failures.delete;});page.once('dialog',dialog=>dialog.accept());await button(item,'삭제').click();await item.waitFor({state:'detached'});
    assert.deepEqual(await page.evaluate(()=>watchFixture.unexpected),[]);assert.deepEqual(page.errors,[]);
});

test('직접 감시 추가 응답이 늦어도 이후 입력한 새 주소를 지우지 않는다',async t=>{
    const page=await fixture(t);
    await page.getByText('확인할 웹페이지가 없습니다. 위에서 주소를 추가하세요.',{exact:true}).waitFor();
    await page.getByLabel('이름',{exact:true}).fill('먼저 보낸 정책');await page.getByLabel('정책 페이지 URL',{exact:true}).fill('https://example.org/first');await page.getByLabel('범위(태그)',{exact:true}).fill('영유아');
    await page.evaluate(()=>{watchFixture.holds.add=true;});await button(page,'추가').evaluate(el=>{el.click();el.click();});assert.equal(await page.evaluate(()=>watchFixture.calls.add.length),1);
    assert.equal(await button(page,'추가').isDisabled(),true);
    await page.getByLabel('이름',{exact:true}).fill('다음에 보낼 정책');await page.getByLabel('정책 페이지 URL',{exact:true}).fill('https://example.org/next');
    await page.evaluate(()=>window.finishWatch('add'));await row(page,'먼저 보낸 정책').waitFor();
    assert.equal(await page.getByLabel('정책 페이지 URL',{exact:true}).inputValue(),'https://example.org/next','이전 요청의 성공이 새 입력을 지우면 안 됨');
    assert.equal(await page.getByLabel('범위(태그)',{exact:true}).inputValue(),'영유아','수정하지 않은 필드도 새 초안과 함께 보존');
    await button(page,'추가').click();await page.getByLabel('이름',{exact:true}).fill('이름만 바꾼 새 초안');
    await page.evaluate(()=>window.finishWatch('add'));await row(page,'다음에 보낼 정책').waitFor();
    assert.equal(await page.getByLabel('이름',{exact:true}).inputValue(),'이름만 바꾼 새 초안');assert.equal(await page.getByLabel('정책 페이지 URL',{exact:true}).inputValue(),'https://example.org/next');assert.equal(await page.getByLabel('범위(태그)',{exact:true}).inputValue(),'영유아');
});

test('직접 감시 변경 비교는 먼저 요청한 느린 응답이 나중에 선택한 정책을 덮지 않는다',async t=>{
    const page=await fixture(t,'seed');await row(page,'시험 정책 1').waitFor();
    await page.evaluate(()=>{watchFixture.holds['diff-1']=true;});await button(row(page,'시험 정책 1'),'변경 내용 보기').click();await button(row(page,'시험 정책 2'),'변경 내용 보기').click();
    await page.getByRole('dialog',{name:'시험 정책 2 — 변경 내용',exact:true}).waitFor();
    await page.evaluate(()=>window.finishWatch('diff-1'));
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole('dialog',{name:'시험 정책 2 — 변경 내용',exact:true}).count(),1,'최신 선택의 변경 비교 유지');
    await button(page,'변경 내용 닫기').click();
    await button(row(page,'시험 정책 1'),'변경 내용 보기').click();await button(row(page,'시험 정책 2'),'변경 내용 보기').click();
    await button(page,'변경 내용 닫기').click();
    await page.evaluate(()=>window.finishWatch('diff-1'));await page.waitForTimeout(100);
    assert.equal(await page.getByRole('dialog').count(),0,'닫은 비교를 늦은 응답이 다시 열지 않음');
});

test('직접 감시 비교는 키보드로 열고 닫으며 입력 이름·포커스 가두기·복귀를 유지한다',async t=>{
    const page=await fixture(t,'seed');
    for(const name of ['이름','정책 페이지 URL','범위(태그)'])assert.equal(await page.getByRole('textbox',{name,exact:true}).count(),1);
    const trigger=button(row(page,'시험 정책 1'),'변경 내용 보기');await trigger.focus();await page.keyboard.press('Enter');
    const dialog=page.getByRole('dialog',{name:'시험 정책 1 — 변경 내용',exact:true});await dialog.waitFor();assert.equal(await dialog.getAttribute('aria-modal'),'true');
    const close=button(dialog,'변경 내용 닫기');assert.equal(await close.evaluate(el=>el===document.activeElement),true);
    await page.keyboard.press('Shift+Tab');assert.equal(await dialog.getByRole('link',{name:'원본 페이지 열기 ↗',exact:true}).evaluate(el=>el===document.activeElement),true);
    await page.keyboard.press('Tab');assert.equal(await close.evaluate(el=>el===document.activeElement),true);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await trigger.evaluate(el=>el===document.activeElement),true);
    await page.keyboard.press('Enter');await dialog.waitFor();await button(dialog,'변경 내용 닫기').click();assert.equal(await trigger.evaluate(el=>el===document.activeElement),true);
    assert.deepEqual(await page.evaluate(()=>watchFixture.unexpected),[]);assert.deepEqual(page.errors,[]);
});

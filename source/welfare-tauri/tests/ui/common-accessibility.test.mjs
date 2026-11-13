import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin='http://127.0.0.1:1420';
async function fixture(t,options={}){
    const browser=await chromium.launch({channel:'msedge',headless:true});t.after(()=>browser.close());
    const page=await browser.newPage({viewport:{width:582,height:850},...options});
    page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    await page.goto(`${origin}/tests/ui/common-accessibility.html`);return page;
}
const focused=locator=>locator.evaluate(element=>element===document.activeElement);

test('도움말은 기존 설명·클릭을 유지하고 키보드 연결·Escape·호버 유지·비활성 안내를 제공한다',async t=>{
    const page=await fixture(t);const action=page.getByRole('button',{name:'도움말 있는 동작',exact:true});
    await action.focus();let tooltip=page.getByRole('tooltip',{name:'선택만으로 저장되지 않습니다.'});await tooltip.waitFor();
    const tooltipBox=await tooltip.boundingBox();assert.ok(tooltipBox.x>=8&&tooltipBox.x+tooltipBox.width<=574,'설명은 좁은 화면 안에서 읽을 수 있음');
    assert.equal(await tooltip.evaluate(element=>getComputedStyle(element).fontSize),'14px');
    assert.ok((await action.getAttribute('aria-describedby')).split(' ').includes('existing-description'));
    assert.ok((await action.getAttribute('aria-describedby')).split(' ').includes(await tooltip.getAttribute('id')));
    await page.keyboard.press('Escape');await tooltip.waitFor({state:'hidden'});assert.equal(await focused(action),true);
    await page.keyboard.press('Enter');assert.equal(await page.evaluate(()=>accessibilityFixture.actions),1);
    await page.getByRole('button',{name:'배경 동작'}).focus();await action.hover();await tooltip.waitFor();const hoverBox=await tooltip.boundingBox();await page.mouse.move(hoverBox.x+hoverBox.width/2,hoverBox.y+hoverBox.height/2);
    await page.waitForTimeout(220);assert.equal(await tooltip.isVisible(),true,'마우스를 설명 위로 옮겨도 읽을 수 있음');
    await page.mouse.move(580,840);await tooltip.waitFor({state:'hidden'});
    const disabled=page.getByRole('button',{name:'비활성 동작'});await disabled.locator('..').focus();
    await page.getByRole('tooltip',{name:'저장이 끝난 후 다시 사용할 수 있습니다.'}).waitFor();
    await page.keyboard.press('Enter');assert.equal(await disabled.isDisabled(),true);assert.equal(await page.evaluate(()=>accessibilityFixture.actions),1);
    await page.getByText('수집 방식 안내',{exact:true}).locator('..').focus();
    await page.getByRole('tooltip',{name:'기본 링크를 탐색하는 방식입니다.'}).waitFor();assert.deepEqual(page.errors,[]);
});

test('동작 줄이기 설정은 버튼 축소를 없애며 명시한 제출 방식·ref·터치 크기를 유지한다',async t=>{
    const page=await fixture(t,{reducedMotion:'reduce',hasTouch:true});const action=page.getByRole('button',{name:'도움말 있는 동작',exact:true});
    await action.waitFor();const box=await action.boundingBox();assert.ok(box.height>=44);
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.waitForTimeout(120);
    assert.equal(await action.evaluate(element=>getComputedStyle(element).transform),'none');await page.mouse.up();
    await page.getByRole('button',{name:'양식 제출'}).click();assert.equal(await page.evaluate(()=>accessibilityFixture.submits),1);
    await page.getByRole('button',{name:'앞 버튼으로 초점'}).click();assert.equal(await focused(action),true);assert.equal(await page.evaluate(()=>accessibilityFixture.submits),1);
    assert.deepEqual(page.errors,[]);
});

test('상세창은 오류에도 이름을 제공하고 숨은 요소를 건너뛰며 초점·Escape·동작 줄이기를 유지한다',async t=>{
    const page=await fixture(t,{reducedMotion:'reduce'});const trigger=page.getByRole('button',{name:'상세 열기',exact:true});await trigger.click();
    let dialog=page.getByRole('dialog',{name:'키보드로 확인하는 복지 안내'});await dialog.waitFor();
    const close=dialog.getByRole('button',{name:'상세 정보 닫기'});assert.equal(await focused(close),true);
    const size=await close.boundingBox();assert.ok(size.width>=44&&size.height>=44);
    assert.ok(['none','matrix(1, 0, 0, 1, 0, 0)'].includes(await dialog.evaluate(element=>getComputedStyle(element).transform)));
    await dialog.evaluate(element=>{const hidden=document.createElement('div');hidden.style.display='none';hidden.innerHTML='<button type="button">숨은 시험 버튼</button>';element.append(hidden);});
    await page.keyboard.press('Shift+Tab');assert.equal(await focused(dialog.getByRole('link',{name:'원문 보기',exact:true})),true);
    await page.keyboard.press('Tab');assert.equal(await focused(close),true);
    await page.evaluate(()=>document.getElementById('outside-focus').focus());await page.keyboard.press('Tab');assert.equal(await focused(close),true);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await focused(trigger),true);
    const errorTrigger=page.getByRole('button',{name:'오류 상세 열기'});await errorTrigger.click();
    dialog=page.getByRole('dialog',{name:'상세 정보를 불러오지 못했습니다',exact:true});await dialog.waitFor();
    await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await focused(errorTrigger),true);
    assert.equal(await page.evaluate(()=>document.body.style.overflow),'');assert.deepEqual(await page.evaluate(()=>accessibilityFixture.similarCalls),[1]);assert.deepEqual(page.errors,[]);
});

test('승인 대화상자는 취소부터 초점을 시작하고 취소·승인 후 원래 버튼으로 복귀한다',async t=>{
    const page=await fixture(t);const trigger=page.getByRole('button',{name:'승인 대화상자 열기'});await trigger.focus();await page.keyboard.press('Enter');
    const dialog=page.getByRole('dialog',{name:'시험 동작을 실행할까요?'});await dialog.waitFor();const cancel=dialog.getByRole('button',{name:'취소'});assert.equal(await focused(cancel),true);
    await page.keyboard.press('Tab');assert.equal(await focused(dialog.getByRole('button',{name:'확인하고 실행'})),true);
    await page.keyboard.press('Tab');assert.equal(await focused(cancel),true);
    await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await focused(trigger),true);assert.deepEqual(await page.evaluate(()=>accessibilityFixture.answers),[false]);
    await page.keyboard.press('Enter');await dialog.waitFor();await dialog.getByRole('button',{name:'확인하고 실행'}).click();await dialog.waitFor({state:'detached'});
    assert.equal(await focused(trigger),true);assert.deepEqual(await page.evaluate(()=>accessibilityFixture.answers),[false,true]);assert.deepEqual(page.errors,[]);
});

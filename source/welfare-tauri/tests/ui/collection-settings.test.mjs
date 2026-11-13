import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
async function pageFor(browser,path,width=850){
    const page=await browser.newPage({viewport:{width,height:1000}});
    page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
    await page.goto(`${origin}/tests/ui/${path}`);return page;
}
const button=(scope,name)=>scope.getByRole('button',{name,exact:true});

test('보관 영역은 선택적으로 열고 생성·실행 없이 요약과 기록을 제공한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/collection-settings',{recursive:true});
    try{
        for(const width of [582,850,1200]){
            const page=await pageFor(browser,'settings-drafts.html?mode=conditions',width);
            await page.getByRole('tab',{name:'수집 설정',exact:true}).click();
            const section=page.getByRole('region',{name:'저장한 다른 조건',exact:true});
            await button(page,'수집 조건 변경').waitFor();
            assert.equal(await page.getByText('이전 수집 설정',{exact:true}).count(),0);
            assert.equal(await page.evaluate(()=>window.__fixtureConditionState().profileReads),0);
            const toggle=button(section,'저장한 다른 조건');
            assert.equal(await toggle.getAttribute('aria-expanded'),'false');
            await toggle.click();
            const list=section.getByRole('list',{name:'보관한 수집 조건',exact:true});
            await list.getByText('이전에 보관한 수집 조건',{exact:true}).waitFor();
            assert.equal(await button(section,'새 조건 만들기').count(),1);
            assert.equal(await button(list,'수집 진행').count(),0);
            assert.equal(await button(list,'조건 수정').count(),2);
            assert.equal(await button(list,'분석 기록 보기').count(),3);
            for(const dark of [false,true]){
                await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
                assert.equal(await list.evaluate(el=>el.scrollWidth>el.clientWidth),false);
                await section.screenshot({path:`../artifacts/collection-settings/conditions-${dark?'dark':'light'}-${width}.png`,animations:'disabled'});
            }
            await button(list,'조건 수정').first().click();
            await section.getByLabel('시·도',{exact:true}).selectOption('서울특별시');
            await section.getByLabel('시·군·구 (선택)',{exact:true}).fill('도봉구');
            await toggle.click();await toggle.click();
            assert.equal(await section.getByLabel('시·군·구 (선택)',{exact:true}).inputValue(),'도봉구');
            assert.equal(await page.evaluate(()=>window.__fixtureConditionState().writes),0);
            assert.deepEqual(page.errors,[]);await page.close();
        }
    }finally{await browser.close();}
});

test('보관한 조건을 수정해도 현재 수집과 예약 설정은 바뀌지 않는다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await pageFor(browser,'settings-drafts.html?mode=conditions');
        await page.getByRole('tab',{name:'수집 설정',exact:true}).click();
        const section=page.getByRole('region',{name:'저장한 다른 조건',exact:true});
        await button(page,'수집 조건 변경').waitFor();
        const before=await page.evaluate(()=>({state:window.__fixtureConditionState(),config:window.__fixtureReadConfig()}));
        await button(section,'저장한 다른 조건').click();
        await button(section,'조건 수정').first().click();
        const edit=section.getByRole('region',{name:'수집 조건 수정',exact:true});
        await button(edit,'다음').click();await button(edit,'다음').click();
        await edit.getByRole('radiogroup',{name:'종료 나이 제한 선택'}).getByRole('radio',{name:'나이 지정',exact:true}).check();
        await edit.getByLabel('종료 나이 추가 개월',{exact:true}).fill('11');
        await button(edit,'다음').click();await button(edit,'최종 저장').click();
        await button(page,'저장 응답 완료').click();
        const after=await page.evaluate(()=>({state:window.__fixtureConditionState(),config:window.__fixtureReadConfig()}));
        assert.deepEqual(after.state.defaultProfile,before.state.defaultProfile);
        assert.deepEqual(after.config,before.config);
        assert.equal(after.state.profiles.one.max_age_months,11);
        assert.deepEqual(page.errors,[]);
    }finally{await browser.close();}
});

test('나이 제한 선택은 0개월과 무제한을 구별하고 수정·상한·키보드·잠금을 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/collection-settings',{recursive:true});
    try{
        const page=await pageFor(browser,'optional-age.html');
        const group=label=>page.getByRole('radiogroup',{name:`${label} 제한 선택`,exact:true});
        const radio=(label,name)=>group(label).getByRole('radio',{name,exact:true});
        const values=async()=>JSON.parse(await page.getByTestId('ages').textContent());
        assert.equal(await radio('시작 나이','제한 없음').isChecked(),true);assert.equal(await radio('종료 나이','나이 지정').isChecked(),true);
        await radio('시작 나이','나이 지정').check();assert.deepEqual(await values(),{min:0,max:11});
        await page.getByText('개월부터',{exact:true}).waitFor();await page.getByText('개월까지',{exact:true}).waitFor();
        await page.getByLabel('시작 나이 추가 개월',{exact:true}).fill('11');assert.deepEqual(await values(),{min:11,max:11});
        await page.getByLabel('시작 나이 추가 개월',{exact:true}).fill('0');await page.getByLabel('시작 나이 만 나이',{exact:true}).fill('1');assert.equal((await values()).min,12);
        await button(page,'저장한 나이 다시 읽기').click();assert.deepEqual(await values(),{min:0,max:1799});
        assert.equal(await page.getByLabel('종료 나이 만 나이',{exact:true}).inputValue(),'149');assert.equal(await page.getByLabel('종료 나이 추가 개월',{exact:true}).inputValue(),'11');
        await page.getByLabel('종료 나이 만 나이',{exact:true}).fill('150');assert.equal((await values()).max,1800);assert.equal(await page.getByLabel('종료 나이 추가 개월',{exact:true}).inputValue(),'0');
        await page.getByLabel('종료 나이 만 나이',{exact:true}).fill('-1');assert.equal((await values()).max,0);
        await page.getByLabel('종료 나이 추가 개월',{exact:true}).fill('12');assert.equal((await values()).max,11);
        await radio('종료 나이','제한 없음').check();assert.equal((await values()).max,'');assert.equal(await page.getByLabel('종료 나이 만 나이',{exact:true}).count(),0);
        await button(page,'저장한 무제한 다시 읽기').click();assert.deepEqual(await values(),{min:null,max:null});
        await radio('시작 나이','제한 없음').focus();await page.keyboard.press('ArrowRight');assert.equal((await values()).min,0);assert.equal(await radio('시작 나이','나이 지정').isChecked(),true);
        await button(page,'입력 잠금 전환').click();assert.equal(await radio('시작 나이','제한 없음').isDisabled(),true);assert.equal(await page.getByLabel('시작 나이 만 나이',{exact:true}).isDisabled(),true);
        await button(page,'입력 잠금 전환').click();
        for(const width of [582,850,1200])for(const dark of [false,true]){
            await page.setViewportSize({width,height:800});await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
            const colors=await group('시작 나이').locator('label').evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).backgroundColor));assert.notEqual(colors[0],colors[1]);
            await page.screenshot({path:`../artifacts/collection-settings/age-${dark?'dark':'light'}-${width}.png`,animations:'disabled'});
        }
        assert.deepEqual(page.errors,[]);
    }finally{await browser.close();}
});

test('Windows 예약 해제는 확실한 완료만 성공으로 표시하고 조회 불명 상태에서 변경하지 않는다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await pageFor(browser,'settings-drafts.html');
        await page.evaluate(()=>{window.__fixtureOs.readFailure=true;});
        await page.getByRole('tab',{name:'자동화/알림',exact:true}).click();
        const unknown=button(page,'등록 상태 확인 필요');await unknown.waitFor();assert.equal(await unknown.isDisabled(),true);
        assert.equal(await button(page,'자동 실행 등록').count(),0);
        await page.evaluate(()=>{window.__fixtureOs.readFailure=false;});
        await button(page,'등록 상태 다시 확인').click();await button(page,'자동 실행 해제').waitFor();
        await page.evaluate(()=>{window.__fixtureOs.deleteResult={status:'success',registered:true};});
        await button(page,'자동 실행 해제').evaluate(el=>{el.click();el.click();});
        await page.getByText('해제 결과를 확인하지 못했습니다. 현재 등록 상태를 다시 확인해주세요.',{exact:true}).waitFor();
        assert.equal(await page.getByText('Windows 자동 실행 작업이 해제되었습니다.',{exact:true}).count(),0);
        assert.equal(await page.evaluate(()=>window.__fixtureOs.deletes),1);
        await page.evaluate(()=>{window.__fixtureOs.deleteResult=null;window.__fixtureOs.deleteFailure=true;window.__fixtureOs.readFailure=true;});
        await button(page,'자동 실행 해제').click();await unknown.waitFor();assert.equal(await unknown.isDisabled(),true);
        assert.equal(await page.getByText('Windows 자동 실행 작업이 해제되었습니다.',{exact:true}).count(),0);
        await page.evaluate(()=>{window.__fixtureOs.deleteFailure=false;window.__fixtureOs.readFailure=false;});
        await button(page,'등록 상태 다시 확인').click();await button(page,'자동 실행 해제').click();
        await page.getByText('Windows 자동 실행 작업이 해제되었습니다.',{exact:true}).waitFor();await button(page,'자동 실행 등록').waitFor();
        assert.equal(await page.evaluate(()=>window.__fixtureOs.deletes),3);assert.equal(await page.evaluate(()=>window.__fixtureOs.registers),0);
        assert.deepEqual(page.errors,[]);
    }finally{await browser.close();}
});

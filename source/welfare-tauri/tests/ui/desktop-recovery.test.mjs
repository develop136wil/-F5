import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('종료 상태 IPC가 응답하지 않아도 시간이 갱신되고 재시도할 수 있다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage();
        await page.goto('http://127.0.0.1:1420/tests/ui/desktop-recovery.html');
        await page.waitForFunction(()=>!!window.requestClose);
        await page.evaluate(()=>{window.testStatus={phase:'ready'};});
        await page.getByRole('textbox').waitFor();
        await page.evaluate(()=>{window.hangStatus=true;window.requestClose();});
        await page.getByText('1초 대기 중',{exact:true}).waitFor();
        assert.deepEqual(await page.evaluate(()=>window.closeAcks),[1], 'slow status cannot trigger native fallback');
        const retry=page.getByRole('button',{name:'종료 다시 확인',exact:true});
        await retry.waitFor({timeout:15000});
        assert.equal(await page.evaluate(()=>window.shutdownCalls),0);
        // A late status response must not close the app after the timeout.
        await page.evaluate(()=>{window.resolveStatus?.({phase:'ready',busy:false});window.hangStatus=false;});
        await retry.click();
        await page.waitForFunction(()=>window.shutdownCalls===1);
    } finally { await browser.close(); }
});

test('실제 연결 화면 컴포넌트는 단절 중 입력을 보존하고 새 조작을 막는다', async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:850,height:650}});
        const errors=[];
        page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/*',route=>{
            const u=new URL(route.request().url());
            return u.origin==='http://127.0.0.1:1420' && !u.pathname.startsWith('/api/') ? route.continue() : route.abort();
        });
        await page.goto('http://127.0.0.1:1420/tests/ui/desktop-recovery.html');
        await page.getByText('복지F5를 준비하고 있어요',{exact:true}).waitFor();
        assert.equal(await page.locator('input').count(),0);
        await page.evaluate(()=>{window.testStatus={phase:'ready'};});
        const input=page.getByRole('textbox',{name:'시험 입력'});
        await input.fill('저장하지 않은 시험 내용');
        await page.evaluate(()=>{window.testStatus={phase:'ready',busy:true};window.requestClose();});
        await page.getByRole('button',{name:'창 유지하기',exact:true}).click();
        assert.equal(await input.evaluate(el=>document.activeElement===el),true,'종료 취소 후 입력 초점 복원');
        assert.equal(await page.evaluate(()=>window.shutdownCalls),0);
        await page.evaluate(()=>window.requestClose());
        await page.getByRole('button',{name:'창 유지하기',exact:true}).waitFor();
        await page.keyboard.press('Escape');
        assert.equal(await input.evaluate(el=>document.activeElement===el),true,'Escape 취소도 초점 복원');
        assert.equal(await input.inputValue(),'저장하지 않은 시험 내용');
        for (const rejected of [true,false]) {
            await page.evaluate(reject=>{window.rejectStatus=reject;window.testStatus={phase:'starting'};},rejected);
            await page.getByText('연결 상태를 확인하고 있어요',{exact:true}).waitFor();
            assert.equal(await page.getByRole('dialog',{name:'연결 상태를 확인하고 있어요'}).evaluate(el=>el.contains(document.activeElement)),true);
            assert.equal(await page.locator('input').inputValue(),'저장하지 않은 시험 내용');
            assert.equal(await page.locator('main').evaluate(el=>!!el.closest('[inert]')),true);
            await page.keyboard.press('Tab');
            await page.keyboard.press('Enter');
            await page.keyboard.press('Shift+Tab');
            assert.equal(await page.getByRole('dialog',{name:'연결 상태를 확인하고 있어요'}).evaluate(el=>el.contains(document.activeElement)),true);
            assert.equal(await page.evaluate(()=>window.actions),0);
            await page.evaluate(()=>{window.rejectStatus=false;window.testStatus={phase:'ready'};});
            await input.waitFor();
            assert.equal(await input.evaluate(el=>document.activeElement===el),true,'복구 후 입력하던 곳으로 초점 복원');
            assert.equal(await input.inputValue(),'저장하지 않은 시험 내용');
        }
        await page.getByRole('button',{name:'시험 작업',exact:true}).click();
        assert.equal(await page.evaluate(()=>window.actions),1);
        assert.equal(await page.evaluate(()=>window.mounts),1);
        await page.evaluate(()=>{window.testStatus={phase:'error',can_exit:true,message:'시험 오류'};});
        await page.getByText('실행 상태를 확인해주세요',{exact:true}).waitFor();
        await page.getByRole('button',{name:'이 창 닫기',exact:true}).click();
        await page.waitForFunction(()=>window.shutdownCalls===1);
        await page.evaluate(()=>{window.requestClose();window.requestClose();});
        assert.equal(await page.evaluate(()=>window.shutdownCalls),1,'종료 대기 중 중복 창 닫기 요청 무시');
        assert.equal(await page.getByRole('dialog').count(),1,'종료 안내 뒤 연결 안내는 접근성 탐색에서 제외');
        assert.deepEqual(errors,[]);
    } finally { await browser.close(); }
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {createServer} from 'vite';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../../',import.meta.url));
const output = process.env.UI_TEST_ARTIFACT_DIR
    ? join(process.env.UI_TEST_ARTIFACT_DIR, 'policy-full-check')
    : fileURLToPath(new URL('../../../artifacts/policy-full-check-ui-20260917/',import.meta.url));
const buttonName = '정책 정보 최신인지 확인하기';

test('통합 점검은 한 번의 사전 확인과 진행 표시로 취소·이어하기·재진입을 처리한다', {timeout:240000}, async () => {
    const server = process.env.UI_TEST_ORIGIN ? null : await createServer({root,envDir:false,envPrefix:[],server:{host:'127.0.0.1',port:0,strictPort:false}});
    let browser;
    try {
        await server?.listen();
        const origin = process.env.UI_TEST_ORIGIN || `http://127.0.0.1:${server.httpServer.address().port}`;
        browser = await chromium.launch({channel:'msedge',headless:true});
        await mkdir(output,{recursive:true});
        const errors=[],blocked=[];
        const open = async (mode='',width=1280) => {
            const page = await browser.newPage({viewport:{width,height:850},serviceWorkers:'block'});
            page.setDefaultTimeout(10000);
            page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',route=>{
                const url=new URL(route.request().url());
                if(url.origin===origin&&!url.pathname.startsWith('/api/')) return route.continue();
                blocked.push(url.pathname);return route.abort();
            });
            await page.goto(`${origin}/tests/ui/policy-full-check.html?mode=${mode}`,{timeout:60000,waitUntil:'domcontentloaded'});
            await page.getByRole('button',{name:buttonName,exact:true}).waitFor({timeout:60000});
            await page.waitForFunction(()=>window.fullCalls.some(call=>call.key==='getPolicyChanges'));
            return page;
        };
        const page = await open();
        let dialogs=0;
        page.once('dialog',async dialog=>{
            dialogs++;
            assert.match(dialog.message(),/최초 비교 2건.*저장된 제안 1건.*문장 다듬기 3건/);
            assert.match(dialog.message(),/정확한 AI 호출 횟수가 아닙니다/);
            assert.doesNotMatch(dialog.message(),/잔여 한도|한도에 도달|20회/);
            assert.match(dialog.message(),/같은 버튼으로 남은 작업을 이어서/);
            await dialog.dismiss();
        });
        await page.getByRole('button',{name:buttonName,exact:true}).click();
        await page.getByRole('button',{name:buttonName,exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:buttonName,exact:true}).isDisabled(),false);
        assert.equal(dialogs,1);
        assert.equal(await page.evaluate(()=>window.fullCalls.filter(call=>call.key==='runPolicyFullCheck').length),0);
        page.once('dialog',async dialog=>{dialogs++;await dialog.accept();});
        await page.getByRole('button',{name:buttonName,exact:true}).click();
        const progress=page.locator('[data-policy-full-check]');
        await progress.getByText('통합 불러오기 중',{exact:true}).waitFor();
        assert.equal(dialogs,2);
        await page.evaluate(()=>{window.fullStatus={};});
        await page.getByText('진행 상태를 확인하지 못했습니다. 연결을 다시 확인하고 있습니다. 작업이 중단됐다는 뜻은 아닙니다.',{exact:true}).waitFor();
        assert.match(await progress.innerText(),/통합 불러오기 중/);
        assert.equal(await page.getByRole('button',{name:buttonName,exact:true}).isDisabled(),true);
        for(const [phase,label] of [['recompute','저장된 제안 다시 점검'],['check','최초·변경 내용 비교'],['style','제안 문장 다듬기'],['embedding','정책 제목 검색 데이터 정리']]) {
            await page.evaluate(phase=>{window.fullStatus={running:true,state:'running',phase,message:'현재 단계 처리 중',checked:2,total:3};},phase);
            await progress.getByText(label,{exact:false}).waitFor();
            assert.match(await progress.innerText(),/2\/3건/);
            assert.equal(await page.getByText(/중복 하위 단계/).count(),0);
        }
        await page.evaluate(()=>{window.fullStatus={running:false,state:'paused',phase:'style',message:'AI 한도로 다음 실행에서 이어서 처리합니다.'};});
        await progress.getByText('점검 일시 중지',{exact:true}).waitFor();
        assert.match(await progress.innerText(),/완료된 작업은 저장/);
        assert.match(await progress.innerText(),/남은 작업을 이어서/);
        await page.screenshot({path:`${output}/paused-1280.png`});
        await page.getByRole('link',{name:'다른 화면',exact:true}).click();
        await page.getByRole('heading',{name:'다른 시험 화면',exact:true}).waitFor();
        await page.getByRole('link',{name:'정책 화면',exact:true}).click();
        await progress.getByText('점검 일시 중지',{exact:true}).waitFor();
        assert.equal(await progress.count(),1);
        page.once('dialog',async dialog=>{dialogs++;await dialog.accept();});
        await page.getByRole('button',{name:buttonName,exact:true}).click();
        await progress.getByText('통합 불러오기 중',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>window.fullCalls.filter(call=>call.key==='runPolicyFullCheck').length),2);
        await page.evaluate(()=>{window.fullStatus={running:false,state:'completed',phase:'done',message:'변경 제안을 준비했습니다. 검토 후 반영해주세요.'};});
        await progress.getByText('점검 완료',{exact:true}).waitFor();
        assert.equal(await page.getByText(/중복 하위 단계/).count(),0);
        assert.ok((await page.evaluate(()=>window.fullCalls)).every(call=>call.key.startsWith('get')||call.key==='runPolicyFullCheck'));
        await page.close();

        for(const mode of ['error','invalid']) {
            const failed=await open(mode);
            failed.on('dialog',async dialog=>{throw Error(`Unexpected dialog: ${dialog.message()}`);});
            await failed.getByRole('button',{name:buttonName,exact:true}).click();
            await failed.getByText(/점검 시작 실패:/).waitFor();
            assert.equal(await failed.evaluate(()=>window.fullCalls.filter(call=>call.key==='runPolicyFullCheck').length),0);
            await failed.close();
        }
        const routine=await open('noai');
        let routineDialogs=0;routine.on('dialog',async dialog=>{routineDialogs++;await dialog.dismiss();});
        await routine.getByRole('button',{name:buttonName,exact:true}).click();
        await routine.locator('[data-policy-full-check]').waitFor();
        assert.equal(routineDialogs,0);await routine.close();
        const empty=await open('empty');
        let emptyDialogs=0;empty.once('dialog',async dialog=>{emptyDialogs++;assert.match(dialog.message(),/문장 다듬기는 건너뜁니다/);await dialog.dismiss();});
        await empty.getByRole('button',{name:buttonName,exact:true}).click();
        assert.equal(emptyDialogs,1);await empty.close();
        for(const width of [582,1280]) {
            const resume=await open('resume',width);
            await resume.locator('[data-policy-full-check]').getByText('통합 문장 정리 중',{exact:true}).waitFor();
            assert.equal(await resume.getByText(/중복 하위 단계/).count(),0);
            await resume.evaluate(()=>{document.documentElement.style.fontSize='20px';});
            assert.equal(await resume.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
            await resume.screenshot({path:`${output}/running-${width}.png`});
            await resume.close();
        }
        for(const [mode,expected] of [
            ['embedding-failed','제목 검색 데이터 준비 완료 0건 · 실패 1건'],
            ['analysis-failed','분석 실패 1'],
        ]) {
            const partial=await open(mode,582);
            const status=partial.locator('[data-policy-full-check]');
            await status.getByText('일부 점검 완료',{exact:true}).waitFor();
            assert.ok((await status.innerText()).includes(expected));
            assert.match(await status.innerText(),/재시도/);
            assert.equal(await status.locator('details').count(),0,'실제 검토 사유 없이 목록을 만들지 않음');
            assert.equal(await status.getByLabel('점검 작업 확인 필요',{exact:true}).count(),1);
            assert.equal(await status.getByLabel('확인이 필요한 정책 있음',{exact:true}).count(),0);
            assert.doesNotMatch(await status.innerText(),/검토 안내가 있는 항목|확인이 필요한 정책/);
            if(mode==='embedding-failed') assert.match(await status.innerText(),/이미 완료한 정책 비교와 제안은 유지/);
            assert.equal(await partial.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
            await partial.close();
        }
        const held=await open('review-held');
        const heldStatus=held.locator('[data-policy-full-check]');
        await heldStatus.getByText('확인이 필요한 정책 1건',{exact:true}).click();
        await heldStatus.getByText('검토 정책',{exact:true}).waitFor();
        await heldStatus.getByText('원문 확인 필요',{exact:true}).waitFor();
        await held.close();
        assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
    } finally {if(browser) await browser.close();await server?.close();}
});

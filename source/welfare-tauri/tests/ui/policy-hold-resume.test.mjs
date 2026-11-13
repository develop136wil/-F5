import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../../', import.meta.url));
const output = fileURLToPath(new URL('../../../artifacts/policy-hold-ui-20260911/', import.meta.url));

test('완료된 보류 사유는 화면 재진입 후에도 읽기 전용으로 복원된다', {timeout:120000}, async () => {
    const server = await createServer({root, envDir:false, envPrefix:[], server:{host:'127.0.0.1', port:0, strictPort:false}});
    let browser;
    try {
        await server.listen();
        const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
        browser = await chromium.launch({channel:'msedge', headless:true});
        await mkdir(output, {recursive:true});
        for (const mode of ['check','full','audit','mirrored']) for (const width of [582,1280]) {
            const page = await browser.newPage({viewport:{width,height:850},serviceWorkers:'block'});
            page.setDefaultTimeout(8000);
            const errors=[], blocked=[];
            page.on('pageerror', error=>errors.push(error.message));
            await page.route('**/*', route => {
                const url = new URL(route.request().url());
                if(url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
                blocked.push(url.pathname);return route.abort();
            });
            await page.goto(`${origin}/tests/ui/policy-hold-resume.html?mode=${mode}`, {timeout:60000});
            const summary = page.getByText('확인이 필요한 정책 3건', {exact:true});
            await summary.waitFor();
            assert.equal(await summary.count(),1, '같은 통합·개별 점검 보류 안내는 중복하지 않음');
            const details = page.locator('details').filter({has:summary});
            await summary.focus();await page.keyboard.press('Enter');
            await details.getByText('다음 단계:', {exact:true}).first().waitFor();
            assert.equal(await details.locator('li').count(),3);
            assert.match(await details.innerText(),/같은 원문을 반복 점검해도 해결되지 않습니다/);
            assert.match(await details.innerText(),/정책의 상세 출처를 연결/);
            assert.match(await details.innerText(),/전송 가능한 공개 출처/);
            if(mode !== 'audit') await page.getByText(/AI 호출 전 보류 3/).waitFor();
            for(const theme of ['light','dark']) {
                await page.evaluate(theme=>document.documentElement.classList.toggle('dark',theme==='dark'),theme);
                await page.evaluate(()=>{document.documentElement.style.fontSize='20px';});
                if(theme==='dark') assert.equal(await page.locator('main').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(17, 24, 39)', '운영 Layout과 같은 어두운 배경');
                assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth > el.clientWidth),false);
                await details.screenshot({path:`${output}/${mode}-${width}-${theme}.png`,animations:'disabled'});
            }
            await page.getByRole('link',{name:'다른 화면',exact:true}).click();
            await page.getByRole('heading',{name:'격리 시험의 다른 화면'}).waitFor();
            await page.getByRole('link',{name:'정책 화면',exact:true}).click();
            await summary.waitFor();
            await summary.click();
            await details.getByText('다음 단계:', {exact:true}).first().waitFor();
            const closeName = ['full','mirrored'].includes(mode) ? '통합 점검 안내 닫기' : mode === 'audit' ? '최초 비교 안내 닫기' : '정책 점검 안내 닫기';
            const close = page.getByRole('button', {name:closeName, exact:true});
            const box = await close.boundingBox();assert.ok(box.width>=44 && box.height>=44);
            await close.click();assert.equal(await summary.count(),0);
            assert.ok((await page.evaluate(()=>window.fixtureCalls)).every(key=>key.startsWith('get')), '이동과 닫기는 읽기 외 호출 없음');
            await page.reload();await summary.waitFor();
            const calls = await page.evaluate(()=>window.fixtureCalls);
            assert.ok(calls.length>0 && calls.every(key=>key.startsWith('get')), '상태 조회만 수행');
            assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
            await page.close();
        }
        const clean = await browser.newPage();
        await clean.route('**/*', r=>new URL(r.request().url()).origin===origin&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
        await clean.goto(`${origin}/tests/ui/policy-hold-resume.html?mode=clean`);
        await clean.getByRole('heading',{name:'정책 최신화',exact:true}).waitFor();
        assert.equal(await clean.getByText('변경 없음',{exact:true}).count(),0, '과거 정상 완료 배너는 복원하지 않음');
        assert.equal(await clean.getByText('확인이 필요한 정책 3건',{exact:true}).count(),0);
    } finally {if(browser) await browser.close();await server.close();}
});

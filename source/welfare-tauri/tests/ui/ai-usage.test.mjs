import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
test('정상 AI 대기는 중복 표시하지 않고 경고와 작은 유료 안내 링크는 유지한다', async () => {
    const browser = await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/ai-usage-20260906', {recursive:true});
    try {
        for (const [mode, title] of Object.entries({idle:'AI 요청 대기 없음','legacy-local':'AI 요청 대기 없음',waiting:'AI 요청을 순서대로 처리하고 있습니다',cooldown:'AI 호출을 잠시 쉬고 있습니다',daily:'일부 AI 모델의 하루 한도에 도달했습니다',wildcard:'AI 사용 상태 확인 필요',recovery:'AI 사용 기록 확인 필요',error:'AI 상태 확인 필요'})) {
            const page = await browser.newPage({viewport:{width:850,height:650}});
            const errors=[]; page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*', route => {
                const url=new URL(route.request().url());
                return url.origin==='http://127.0.0.1:1420' && !url.pathname.startsWith('/api/') ? route.continue() : route.abort();
            });
            await page.goto(`http://127.0.0.1:1420/tests/ui/ai-usage.html?mode=${mode}`);
            await page.getByRole('link',{name:'Google 유료 이용 안내 ↗',exact:true}).waitFor();
            await page.waitForFunction(()=>window.aiStatusFetched===true);
            if(['idle','legacy-local'].includes(mode)) {
                assert.equal(await page.getByRole('status').count(),0);
                assert.equal(await page.locator('details').count(),0);
            } else {
                await page.getByRole('status').filter({hasText:title}).waitFor();
                assert.equal(await page.locator('details').count(),1,'경고 도움말만 접어서 제공');
                assert.equal(await page.locator('details').getAttribute('open'),null);
            }
            const paid=page.getByRole('link',{name:'Google 유료 이용 안내 ↗',exact:true});
            await paid.focus();
            const tooltip=page.getByRole('tooltip');
            await tooltip.waitFor();
            assert.match(await tooltip.innerText(),/유료 프로젝트.*비용.*결제 설정을 변경하지 않습니다/);
            assert.equal(await page.getByText('앱의 하루 AI 요청 상한에 도달했습니다',{exact:true}).count(),0);
            assert.ok(Number.parseFloat(await paid.evaluate(el=>getComputedStyle(el).fontSize))<=12);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth), false);
            assert.deepEqual(errors, []);
            await page.screenshot({path:`../artifacts/ai-usage-20260906/${mode}.png`,fullPage:true});
            await page.close();
        }
    } finally {await browser.close();}
});

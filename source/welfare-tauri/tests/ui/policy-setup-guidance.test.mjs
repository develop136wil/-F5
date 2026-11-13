import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';

for (const mode of ['정책','보관함']) test(`${mode}: 복제·권한과 주소 등록을 분리하고 단계별 설명·비공개 링크·항목 안내를 유지`,async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        for (const width of [582,850,1100]) {
            const page=await browser.newPage({viewport:{width,height:900}}),errors=[];
            page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*',route=>{
                const url=new URL(route.request().url());
                return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();
            });
            await page.goto(`${origin}/tests/ui/settings-drafts.html`);
            await page.getByRole('link',{name:`${mode} 단계 시험`,exact:true}).click();
            const dialog=page.getByRole('dialog');
            const tab=dialog.getByRole('button',{name:/^4\. 템플릿 복제/});
            await tab.click();assert.equal(await tab.getAttribute('aria-current'),'step');
            assert.equal(await dialog.locator('[data-notion-video]').count(),0,'현재 튜토리얼은 외부 영상 대신 단계별 설명을 사용');
            assert.match(await dialog.locator('#setup-section-template').innerText(),/연결 권한/);
            assert.equal(await dialog.locator('[data-setup-stage-instructions]').count(),1);
            for (const dark of [false,true]) {
                await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
                assert.equal(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth),false);
                assert.equal(await dialog.locator('[data-setup-heading]').evaluate(el=>getComputedStyle(el).fontSize),'16px');
                assert.equal(await dialog.locator('[data-setup-stage-instructions] h4').first().evaluate(el=>getComputedStyle(el).fontSize),'14px');
            }
            await dialog.getByRole('button',{name:/^5\. 주소 등록/}).click();
            assert.equal(await dialog.locator('[data-notion-video]').count(),0);
            const help=dialog.locator('[data-setup-stage-instructions]');
            assert.match(await help.innerText(),/웹에 게시하거나 공개 범위를 바꿀 필요는 없습니다/);
            assert.match(await help.innerText(),/Ctrl\+V.*마우스 오른쪽 버튼 → 붙여넣기/);
            assert.equal(await help.getByRole('link',{name:/링크 복사와 공유 안내/}).getAttribute('href'),'https://www.notion.com/help/sharing-and-permissions');
            const input=mode==='정책'?dialog.locator('input[id^="onboarding-policy-"]').first():dialog.locator('#onboarding-news-db');
            await input.focus();assert.equal(await input.evaluate(el=>el===document.activeElement),true);
            if(mode==='정책') {
                const note=dialog.locator('[data-policy-schema-note]');
                assert.match(await note.innerText(),/15개 항목\(열\)의 이름과 종류는 유지/);
                assert.equal(await note.locator('strong').evaluate(el=>getComputedStyle(el).fontWeight),'600');
            } else assert.equal(await dialog.locator('[data-policy-schema-note]').count(),0);
            assert.equal(await page.evaluate(()=>window.__fixtureConditionState().writes),0);
            assert.deepEqual(errors,[]);await page.close();
        }
    } finally {await browser.close();}
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';

test('온보딩의 주 단계·도움말·현재 행동 위계를 좁은 화면과 다크모드에서도 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/onboarding-hierarchy',{recursive:true});
    try {
        for(const width of [582,850,1100]) {
            const page=await browser.newPage({viewport:{width,height:900}});
            const errors=[];page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',route=>{
                const url=new URL(route.request().url());
                return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();
            });
            await page.goto(`${origin}/tests/ui/settings-drafts.html`);
            await page.getByRole('link',{name:'Gemini 연결하기',exact:true}).click();
            const dialog=page.getByRole('dialog',{name:'연결·설정 도우미'});
            const help=dialog.locator('[data-setup-help]');
            await help.getByRole('heading',{name:'Google AI Studio에서 API 키 확인하기',exact:true}).waitFor();
            await help.locator('img').evaluate(img=>img.complete?undefined:new Promise(resolve=>{img.onload=resolve;img.onerror=resolve;}));
            for(const dark of [false,true]) {
                await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
                const hierarchy=await dialog.evaluate(el=>{
                    const main=el.querySelector('[data-setup-heading]');
                    const help=el.querySelector('[data-setup-help]');
                    const label=el.querySelector('[data-setup-help-label]');
                    const action=el.querySelector('[data-setup-action]');
                    const font=node=>parseFloat(getComputedStyle(node).fontSize);
                    const title=el.querySelector('h1'),section=el.querySelector('h2');
                    return {titleSize:font(title),sectionSize:font(section),mainSize:font(main),labelSize:font(label),actionSize:font(action),
                        titleWeight:getComputedStyle(title).fontWeight,sectionWeight:getComputedStyle(section).fontWeight,
                        mainTag:main.tagName,actionTag:action.tagName,
                        inset:action.getBoundingClientRect().left>label.getBoundingClientRect().left&&label.getBoundingClientRect().left>main.getBoundingClientRect().left,
                        noLines:getComputedStyle(help).borderTopWidth==='0px'&&getComputedStyle(help).borderBottomWidth==='0px',
                        overflow:el.scrollWidth>el.clientWidth};
                });
                assert.ok(hierarchy.mainSize>hierarchy.actionSize&&hierarchy.actionSize>hierarchy.labelSize,JSON.stringify(hierarchy));
                assert.deepEqual([hierarchy.titleSize,hierarchy.sectionSize,hierarchy.mainSize,hierarchy.actionSize,hierarchy.labelSize],[28,20,16,14,12]);
                assert.equal(hierarchy.titleWeight,'700');assert.equal(hierarchy.sectionWeight,'600');
                assert.equal(hierarchy.mainTag,'H3');assert.equal(hierarchy.actionTag,'H4');
                assert.equal(hierarchy.inset,true);assert.equal(hierarchy.noLines,true);assert.equal(hierarchy.overflow,false);
                assert.equal(await dialog.locator('a[href="https://aistudio.google.com/app/apikey"]').count(),1,'중복 API 키 링크 없음');
                await page.screenshot({path:`../artifacts/onboarding-hierarchy/google-${dark?'dark':'light'}-${width}.png`,animations:'disabled'});
            }
            await help.getByRole('button',{name:'다음 설명 보기',exact:true}).click();
            await help.getByRole('heading',{name:'기존 키 확인 또는 새 키 만들기',exact:true}).waitFor();
            await help.getByRole('button',{name:'이전 설명',exact:true}).click();
            await help.getByRole('heading',{name:'Google AI Studio에서 API 키 확인하기',exact:true}).waitFor();
            await help.locator('summary').first().click();
            assert.equal(await help.getAttribute('open'),null);
            await dialog.getByLabel('Gemini API 키 (AI 연결 키)').fill('synthetic-hierarchy-key');
            assert.equal(await dialog.getByLabel('Gemini API 키 (AI 연결 키)').inputValue(),'synthetic-hierarchy-key');
            assert.deepEqual(errors,[]);
            await page.close();
        }
    } finally {await browser.close();}
});

test('Notion 가입과 템플릿 복제는 해당 단계 안에서만 하나의 행동 링크로 제공한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:850,height:900}});
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/*',route=>{
            const url=new URL(route.request().url());
            return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();
        });
        for(const [mode,section,label] of [['정책','정책표 연결','정책표 템플릿 열기'],['보관함','보관함 연결','새 소식 보관함 템플릿 열기']]) {
            await page.goto(`${origin}/tests/ui/settings-drafts.html`);
            await page.getByRole('link',{name:`${mode} 단계 시험`,exact:true}).click();
            const dialog=page.getByRole('dialog',{name:'연결·설정 도우미'});
            if(mode==='보관함') {
                await dialog.getByRole('button',{name:/^2\. Notion 가입/}).click();
                await dialog.getByRole('link',{name:'Notion 가입·로그인 열기 — 새 창으로 열림',exact:true}).waitFor();
                assert.equal(await dialog.locator('a[href="https://www.notion.so/my-integrations"]').count(),0,'가입 안내와 연결 만들기 행동을 동시에 노출하지 않음');
                await dialog.getByRole('button',{name:/작업 공간 확인했어요/}).click();
                await dialog.getByRole('link',{name:'Notion 연결 관리 열기 — 새 창으로 열림',exact:true}).waitFor();
            }
            const tab=dialog.getByRole('button',{name:/^4\. 템플릿 복제/});
            await tab.click();
            assert.equal(await tab.getAttribute('aria-current'),'step');
            const action=dialog.getByRole('link',{name:`${label} — 새 창으로 열림`,exact:true});
            await action.waitFor();
            assert.equal(await action.count(),1);
            const href=await action.getAttribute('href');
            assert.ok(href.startsWith('https://'));
            assert.equal(await dialog.locator('a').evaluateAll((links,href)=>links.filter(a=>a.href===href).length,href),1);
            assert.equal(await action.getAttribute('target'),'_blank');
            assert.match(await action.getAttribute('rel'),/noopener/);
            await dialog.getByRole('button',{name:'영상 접기',exact:true}).waitFor();
            assert.equal(await dialog.locator('[data-notion-video]').count(),1,'복제 단계에 영상 영역을 기본 표시');
            assert.equal(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth),false);
        }
        assert.deepEqual(errors,[]);
    } finally {await browser.close();}
});

test('설정의 페이지·주요 섹션·지역 선택 제목은 일관된 크기와 수준을 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    await mkdir('../artifacts/onboarding-hierarchy',{recursive:true});
    try{
        for(const width of [582,850,1100]){
            const page=await browser.newPage({viewport:{width,height:1000}});
            const errors=[];page.on('pageerror',e=>errors.push(e.message));
            await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
            await page.goto(`${origin}/tests/ui/settings-drafts.html?mode=conditions`);
            const heading=page.getByRole('heading',{name:'설정',level:1,exact:true});
            assert.equal(await heading.evaluate(el=>getComputedStyle(el).fontSize),'28px');
            await page.getByRole('tab',{name:'수집 설정',exact:true}).click();
            const section=page.getByRole('region',{name:'새 소식 수집 조건',exact:true});
            await section.getByRole('button',{name:'수집 조건 변경',exact:true}).click();
            await section.getByLabel('시·도',{exact:true}).selectOption('서울특별시');
            await section.getByLabel('시·군·구 (선택)',{exact:true}).fill('도봉구');
            for(const dark of [false,true]){
                await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
                const main=section.getByRole('heading',{name:'새 소식 수집 조건',level:2,exact:true});
                const group=section.getByRole('heading',{name:'어느 지역의 소식을 찾을까요?',level:3,exact:true});
                assert.equal(await main.evaluate(el=>getComputedStyle(el).fontSize),'20px');
                assert.equal(await group.evaluate(el=>getComputedStyle(el).fontSize),'16px');
                assert.equal(await section.getByLabel('시·군·구 (선택)',{exact:true}).inputValue(),'도봉구');
                assert.equal(await section.evaluate(el=>el.scrollWidth>el.clientWidth),false);
                await section.screenshot({path:`../artifacts/onboarding-hierarchy/region-${dark?'dark':'light'}-${width}.png`,animations:'disabled'});
            }
            assert.equal(await page.evaluate(()=>window.__fixtureConditionState().writes),0,'위계 확인 중 설정 쓰기 없음');
            assert.deepEqual(errors,[]);await page.close();
        }
    }finally{await browser.close();}
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin='http://127.0.0.1:1420';
async function fixture(t,{width=850,height=850,theme=null,restricted=false,collapsed=false,reducedMotion='no-preference'}={}){
    const browser=await chromium.launch({channel:'msedge',headless:true});t.after(()=>browser.close());
    const page=await browser.newPage({viewport:{width,height},reducedMotion});page.errors=[];page.blocked=[];
    page.on('pageerror',error=>page.errors.push(error.message));
    await page.addInitScript(({theme,restricted,collapsed})=>{
        if(restricted){for(const key of ['getItem','setItem'])Object.defineProperty(Storage.prototype,key,{value(){throw new DOMException('Synthetic storage restriction','SecurityError');},configurable:true});}
        else{if(theme!==null)localStorage.setItem('darkMode',theme);localStorage.setItem('sidebar-collapsed',String(collapsed));}
    },{theme,restricted,collapsed});
    await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();page.blocked.push(url.origin+url.pathname);return route.abort();});
    await page.goto(`${origin}/tests/ui/accessibility-shell.html`);await page.getByRole('heading',{name:'시험 홈',exact:true}).waitFor();return page;
}
const focused=locator=>locator.evaluate(element=>element===document.activeElement);
async function isolated(page){assert.deepEqual(page.errors,[]);assert.deepEqual(page.blocked,[]);assert.deepEqual(await page.evaluate(()=>shellFixture.unexpected),[]);}

test('화면의 첫 Tab은 본문 바로가기이며 Enter로 실제 main에 초점을 옮긴다',async t=>{
    const page=await fixture(t);await page.keyboard.press('Tab');const skip=page.getByRole('link',{name:'본문으로 바로가기'});assert.equal(await focused(skip),true);
    const box=await skip.boundingBox();assert.ok(box.y>=0);await page.keyboard.press('Enter');assert.equal(await focused(page.getByRole('main',{name:'본문'})),true);await isolated(page);
});

test('작은 화면 메뉴는 처음·마지막 초점 순환과 Escape 복귀·본문 비활성·확대 후 복구를 유지한다',async t=>{
    const page=await fixture(t,{width:582});const opener=page.getByRole('button',{name:'메뉴 열기',exact:true});await opener.focus();await page.keyboard.press('Enter');
    const menu=page.getByRole('dialog',{name:'메뉴',exact:true}),main=page.getByRole('main',{name:'본문',includeHidden:true});await menu.waitFor();const close=menu.getByRole('button',{name:'메뉴 닫기',exact:true});
    assert.equal(await focused(close),true);assert.equal(await main.evaluate(element=>element.inert),true);assert.equal(await page.locator('header').evaluate(element=>element.inert),true);
    await page.keyboard.press('Shift+Tab');assert.equal(await focused(menu.getByRole('link',{name:'설정',exact:true})),true);await page.keyboard.press('Tab');assert.equal(await focused(close),true);
    await page.keyboard.press('Tab');assert.equal(await focused(menu.getByRole('link',{name:'홈',exact:true})),true);await page.keyboard.press('Escape');await menu.waitFor({state:'hidden'});
    assert.equal(await focused(opener),true);assert.equal(await main.evaluate(element=>element.inert),false);
    await opener.click();await menu.waitFor();await page.setViewportSize({width:850,height:850});await page.waitForFunction(()=>!document.getElementById('main-content').inert);
    assert.equal(await page.getByRole('dialog',{name:'메뉴',exact:true}).count(),0);assert.equal(await page.locator('header').evaluate(element=>element.inert),false);
    await page.getByRole('button',{name:'시험 테마 전환'}).click();assert.equal(await page.getByRole('button',{name:'시험 테마 전환'}).getAttribute('aria-pressed'),'true');await isolated(page);
});

test('접힌 메뉴는 글자 200% 확대에서도 아이콘이 잘리지 않고 키보드 탐색·펼치기가 가능하다',async t=>{
    const page=await fixture(t,{collapsed:true});await page.evaluate(()=>document.documentElement.style.fontSize='200%');
    const sidebar=page.locator('aside#mobile-navigation');const metrics=await sidebar.evaluate(element=>{const bounds=element.getBoundingClientRect();return [...element.querySelectorAll('nav a')].map(link=>{const r=link.getBoundingClientRect(),icon=link.querySelector('svg').getBoundingClientRect();return {label:link.getAttribute('aria-label'),width:r.width,height:r.height,iconLeft:icon.left,iconRight:icon.right,sidebarLeft:bounds.left,sidebarRight:bounds.right};});});
    for(const item of metrics){assert.ok(item.iconLeft>=item.sidebarLeft&&item.iconRight<=item.sidebarRight,`${item.label} 아이콘이 사이드바 안에 유지되어야 함: ${JSON.stringify(item)}`);assert.ok(item.width>=24&&item.height>=44);}
    const search=sidebar.getByRole('link',{name:'통합 검색',exact:true});await search.focus();await page.keyboard.press('Enter');await page.getByRole('heading',{name:'시험 화면 /search'}).waitFor();
    const expand=sidebar.getByRole('button',{name:'메뉴 펼치기'});await expand.focus();await page.keyboard.press('Enter');assert.equal(await sidebar.getAttribute('data-collapsed'),'false');await isolated(page);
});

test('손상되거나 막힌 테마 저장소도 렌더링·전환을 막지 않고 true·false 값은 보존한다',async t=>{
    for(const [theme,restricted,expected]of [['not-json',false,false],['true',false,true],['false',false,false],[null,true,false]]){
        const page=await fixture(t,{theme,restricted});const toggle=page.getByRole('button',{name:'시험 테마 전환'});
        assert.equal(await toggle.getAttribute('aria-pressed'),String(expected));assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('dark')),expected);
        if(!restricted)assert.equal(await page.evaluate(()=>localStorage.getItem('darkMode')),String(expected));
        await toggle.click();assert.equal(await toggle.getAttribute('aria-pressed'),String(!expected));assert.equal(await page.evaluate(()=>document.documentElement.classList.contains('dark')),!expected);
        if(!restricted)assert.equal(await page.evaluate(()=>localStorage.getItem('darkMode')),String(!expected));await isolated(page);await page.close();
    }
});

test('850×700에서 글자 200%인 접힌 메뉴도 진행 상황 버튼이 처음부터 보이며 문서 이동 없이 열린다',async t=>{
    const page=await fixture(t,{width:850,height:700,collapsed:true});await page.evaluate(()=>document.documentElement.style.fontSize='200%');
    const sidebar=page.locator('aside#mobile-navigation'),status=sidebar.getByRole('button',{name:'진행 상황',exact:true});
    const metrics=await status.evaluate(element=>{const rect=element.getBoundingClientRect(),hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);return {top:rect.top,bottom:rect.bottom,width:rect.width,height:rect.height,viewport:innerHeight,scrollY,documentHeight:document.documentElement.scrollHeight,hit:!!hit&&element.contains(hit)};});
    assert.ok(metrics.top>=0&&metrics.bottom<=metrics.viewport,JSON.stringify(metrics));assert.ok(metrics.width>=44&&metrics.height>=44);assert.equal(metrics.hit,true);assert.equal(metrics.scrollY,0);assert.ok(metrics.documentHeight<=metrics.viewport,'문서 전체의 불필요한 세로 스크롤이 없어야 함');
    await sidebar.getByRole('link',{name:'설정',exact:true}).focus();await page.keyboard.press('Tab');assert.equal(await focused(status),true);assert.equal(await page.evaluate(()=>scrollY),0);
    await page.keyboard.press('Enter');assert.equal(await status.getAttribute('aria-expanded'),'true');
    const panel=await page.locator('#sidebar-status').boundingBox();assert.ok(panel.y>=0&&panel.y+panel.height<=700);await isolated(page);
});

test('동작 줄이기 설정에서 실제 상태 아이콘은 반복 회전하지 않고 페이지 이동은 최종 위치로 완료된다',async t=>{
    const page=await fixture(t,{collapsed:true,reducedMotion:'reduce'});await page.evaluate(()=>window.setShellBusy());
    const status=page.getByRole('button',{name:'작업 진행 중 · 진행 상황',exact:true});await status.waitFor();
    const animation=await status.locator('svg').evaluate(element=>({duration:getComputedStyle(element).animationDuration,count:getComputedStyle(element).animationIterationCount}));
    assert.ok(parseFloat(animation.duration)<0.001);assert.equal(animation.count,'1');
    await page.getByRole('link',{name:'시험 페이지 이동'}).click();await page.getByRole('heading',{name:'시험 화면 /search'}).waitFor();
    const motion=await page.locator('[data-shell-page="/search"]').evaluate(element=>{const style=getComputedStyle(element.parentElement);return {opacity:style.opacity,transform:style.transform};});
    assert.equal(motion.opacity,'1');assert.ok(['none','matrix(1, 0, 0, 1, 0, 0)'].includes(motion.transform));assert.equal(await page.locator('[data-shell-page="/"]').count(),0);await isolated(page);
});

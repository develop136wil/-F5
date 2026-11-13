import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('검색·리포트·사이트 관리의 제목 단계와 좁은 화면 위계를 유지한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:900}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
  await mkdir('../artifacts/hierarchy',{recursive:true});
  for(const kind of ['reports','sites','search']) {
   await page.goto(`http://127.0.0.1:1420/tests/ui/page-hierarchy.html?page=${kind}`);
   await page.getByRole('heading',{level:1}).waitFor();
   if(kind==='search') {
    await page.getByRole('textbox',{name:'복지 정보 검색어'}).fill('지원');
    await page.getByRole('button',{name:'검색',exact:true}).click();
    await page.getByRole('heading',{name:'시험 복지 지원 소식',exact:true}).waitFor();
   } else if(kind==='sites') await page.getByRole('heading',{name:'시험 복지 지원 기관',exact:true}).waitFor();
   else await page.getByRole('heading',{name:'수집 통계',exact:true}).waitFor();
   for(const width of [1280,1024,582]) {
    await page.setViewportSize({width,height:900});
    if(width>=768) assert.ok((await page.locator('main').boundingBox()).x>=260,'실제 펼친 사이드바 공간을 제외한 본문 폭으로 검사');
    const headings=await page.locator('main h1,main h2,main h3').evaluateAll(els=>els.filter(el=>el.getClientRects().length).map(el=>({level:Number(el.tagName.slice(1)),size:parseFloat(getComputedStyle(el).fontSize),text:el.textContent})));
    assert.equal(headings.filter(h=>h.level===1).length,1);
    assert.equal(headings[0].size,28);
    for(let i=1;i<headings.length;i++) {
     assert.ok(headings[i].level<=headings[i-1].level+1,`제목 단계 건너뜀: ${headings[i].text}`);
     assert.equal(headings[i].size,headings[i].level===2?20:16,headings[i].text);
    }
    if(kind==='sites') {
     const title=page.getByRole('heading',{name:'시험 복지 지원 기관',exact:true});
     assert.ok((await title.boundingBox()).width>=120,'펼친 사이드바가 있어도 사이트 제목이 읽을 수 있는 폭 유지');
     const action=page.getByRole('button',{name:'시험 복지 지원 기관 수집 규칙 테스트',exact:true});
     await action.focus();
     assert.equal(await action.evaluate(el=>getComputedStyle(el).opacity),'1');
     assert.ok((await action.boundingBox()).y>(await title.boundingBox()).y,'사이트 작업은 제목 옆이 아닌 아래에 배치');
     await page.getByRole('button',{name:'시험 복지 지원 기관 사이트 삭제',exact:true}).focus();
    }
    if(kind==='reports') for(const label of ['카테고리','상태','수집일 (시작)','수집일 (종료)','사이트']) assert.equal(await page.getByLabel(label,{exact:true}).count(),1);
    // Charts resize through ResizeObserver after viewport changes.
    await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth,{},{timeout:5000}).catch(async()=>{
     await page.screenshot({path:`../artifacts/hierarchy/${kind}-${width}-overflow.png`,fullPage:true});
     assert.fail(`${kind} ${width}px 가로 넘침: ${await page.evaluate(()=>JSON.stringify([...document.querySelectorAll('main *')].filter(el=>el.getBoundingClientRect().right>innerWidth).map(el=>({tag:el.tagName,cls:el.getAttribute('class')})).slice(0,6)))}`);
    });
    await page.screenshot({path:`../artifacts/hierarchy/${kind}-${width}.png`,fullPage:true});
   }
   assert.deepEqual(await page.evaluate(()=>window.unexpectedCalls),[]);
  }
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

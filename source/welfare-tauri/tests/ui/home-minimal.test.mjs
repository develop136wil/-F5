import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
test('홈 보조 문구 제거와 주 버튼 크기·정렬을 확인한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1400,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).origin===origin&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
  await page.goto(`${origin}/tests/ui/home-workflow.html`);
  await mkdir('../artifacts/home-beta-0214',{recursive:true});
  for(const width of [582,850,1478]) {
   await page.setViewportSize({width,height:900});
   await page.getByRole('button',{name:`${width}px`,exact:true}).click();
   for(const theme of ['light','dark']) {
   await page.evaluate(value=>document.documentElement.classList.toggle('dark',value==='dark'),theme);
   const main=page.getByRole('region',{name:'주요 업무'});
   const a=main.getByRole('link',{name:'정책 변경 확인하기',exact:true});
   const b=main.getByRole('link',{name:'새 소식 탐색하기',exact:true});
   await a.waitFor();
   assert.ok((await a.boundingBox()).height>=72);
   const cards=await main.locator('article').all();
   const cardBoxes=await Promise.all(cards.map(card=>card.boundingBox()));
   assert.equal(cards.length,2);
   for(const box of cardBoxes) assert.ok(box.height >= (width>=1280?352:304));
   const columns=await main.evaluate(el=>{
    const style=getComputedStyle(el), font=parseFloat(getComputedStyle(document.documentElement).fontSize);
    return {actual:style.gridTemplateColumns.split(' ').filter(track=>parseFloat(track)>0).length,expected:el.clientWidth>=36*font+parseFloat(style.columnGap)?2:1};
   });
   assert.equal(columns.actual,columns.expected,'뷰포트가 아닌 실제 본문 폭과 글자 크기에 따라 재배치');
   if(columns.expected===2) {
    assert.ok(Math.abs((await a.boundingBox()).y-(await b.boundingBox()).y)<2);
    assert.ok(Math.abs(cardBoxes[0].height-cardBoxes[1].height)<2);
   } else assert.ok(cardBoxes[1].y >= cardBoxes[0].y+cardBoxes[0].height);
   const secondary=await page.getByRole('navigation',{name:'바로가기'}).boundingBox();
   for(const shortcut of await page.getByRole('navigation',{name:'바로가기'}).getByRole('link').all()) {
    const icon=await shortcut.locator('svg').boundingBox(),card=await shortcut.boundingBox();
    assert.ok(icon.width >= (width>=1280?112:96));
    assert.ok(icon.x+icon.width/2>card.x+card.width/2 && icon.y<card.y,'주 카드와 같은 우측 상단 장식');
    assert.equal(await shortcut.locator('svg').getAttribute('aria-hidden'),'true');
    assert.equal(await shortcut.locator('svg').evaluate(el=>getComputedStyle(el).pointerEvents),'none');
    assert.equal(await shortcut.locator('span').first().evaluate(el=>getComputedStyle(el).fontWeight),'700');
   }
   assert.ok(cardBoxes[0].height>secondary.height*1.8);
   assert.doesNotMatch(await main.innerText(),/기존 정보 관리|새로운 정보 발견|업무 화면으로 이동합니다|Notion·HTML 저장/);
   assert.equal(await page.locator('#fixture-app main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
   await page.locator('#fixture-app').screenshot({path:`../artifacts/home-beta-0214/home-${theme}-${width}.png`});
   }
  }
  const notice=page.locator('details').filter({hasText:'마감 임박 알림'});
  await notice.locator('summary').click();
  await notice.getByRole('link',{name:'시험 마감 소식',exact:true}).waitFor();
  assert.equal(await notice.locator('li').count(),1);
  assert.equal(await notice.getByText('마감 2026-09-14',{exact:true}).count(),1);
  assert.equal(await notice.locator('h2,h3').count(),0);
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const artifactDir=process.env.UI_TEST_ARTIFACT_DIR || '../artifacts/policy-minimal-20260907';
const origin=process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';
test('정책 일괄 반영 진입점과 세부 작업은 선택 전에도 보인다',async()=>{
 const browser=await chromium.launch({...(process.env.UI_BROWSER_EXECUTABLE?{executablePath:process.env.UI_BROWSER_EXECUTABLE}:{channel:'msedge'}),headless:true});
 try{
  for(const width of [850,1280]){
   const page=await browser.newPage({viewport:{width,height:700}});const errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>new URL(r.request().url()).origin===origin&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
   await page.goto(`${origin}/tests/ui/policy-review.html`);
   const apply=page.getByRole('button',{name:'선택한 내용 Notion에 반영 (0)',exact:true}).first();
   await apply.waitFor();assert.equal(await apply.isDisabled(),true);
   const more=page.getByRole('button',{name:'세부 작업',exact:true});
   await more.click();assert.equal(await more.getAttribute('aria-expanded'),'true');
   const sync=page.getByRole('button',{name:/^Notion 정책 불러오기/});
   assert.match(await sync.innerText(),/필요 시 AI 사용/);
   assert.doesNotMatch(await sync.innerText(),/AI 사용 없음/);
   assert.match(await sync.locator('..').innerText(),/새 정책·제목이 바뀐 정책·검색용 데이터가 없는 정책/);
   assert.match(await page.getByRole('button',{name:/^저장된 변경 제안 다시 점검/}).innerText(),/AI 사용 없음/);
   assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false,'AI 사용 안내가 가로로 넘치지 않아야 함');
   await mkdir(artifactDir,{recursive:true});
   await page.screenshot({path:`${artifactDir}/ai-usage-${width}.png`,fullPage:true});
   await more.click();
   assert.equal(await page.locator('#policy-ai-usage').isVisible(),true,'세부 작업을 닫아도 AI 안내 표시');
   await more.click();
   assert.ok((await more.boundingBox()).height>=44);
   await page.goto(`${origin}/tests/ui/policy-review.html?mode=colours`);
   const names=['의료·재활','교육·보육','돌봄·양육','가족 지원','생활 지원'];
   const colours=[];
   for(const name of names){
    const shelf=page.getByRole('button',{name,exact:true});await shelf.waitFor();
    assert.equal(await shelf.getAttribute('aria-pressed'),'true');
    colours.push(await shelf.evaluate(el=>getComputedStyle(el).backgroundColor));
    assert.equal(await shelf.locator('svg').count(),0);
   }
   assert.equal(new Set(colours).size,1,'카테고리 개수와 무관하게 선택색 통일');
   assert.ok((await page.getByRole('button',{name:'정책 정보 최신인지 확인하기',exact:true}).boundingBox()).height>=48);
   const verify=page.getByRole('button',{name:'연결 확인',exact:true});
   assert.equal(await verify.innerText(),'연결 확인');
   assert.ok((await verify.boundingBox()).height<60,'연결 확인 문구가 세로로 잘리지 않아야 함');
   assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
   await page.locator('#policy-review-1 button[aria-controls="policy-detail-1"]').click();
   const headers=page.locator('#policy-detail-1 h3');
   const blue=await headers.nth(0).evaluate(el=>getComputedStyle(el.parentElement).backgroundColor);
   const orange=await headers.nth(1).evaluate(el=>getComputedStyle(el.parentElement).backgroundColor);
   assert.notEqual(blue,orange);
   await page.screenshot({path:`${artifactDir}/colours-${width}.png`});
   await page.goto(`${origin}/tests/ui/policy-review.html?mode=colours&moreShelves=1`);
   await page.getByRole('button',{name:'추가 보관함 35',exact:true}).waitFor();
   const many=page.locator('button[aria-pressed="true"][title^="로컬 "]');
   assert.equal(await many.count(),40);
   assert.equal((await many.evaluateAll(els=>[...new Set(els.map(el=>getComputedStyle(el).backgroundColor))])).length,1);
   assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false,'40개 보관함도 줄바꿈하며 가로 넘침 없음');
   assert.deepEqual(errors,[]);await page.close();
  }
 }finally{await browser.close();}
});

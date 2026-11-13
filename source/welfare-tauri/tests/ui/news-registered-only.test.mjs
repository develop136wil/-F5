import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const output=process.env.UI_TEST_ARTIFACT_DIR||'../artifacts/news-registered-only-20260917/screenshots';
test('기존 새 소식 화면은 기관 범위와 찾은 소식을 유지하고 과거 복지로 이력은 접어서 보존한다',async()=>{
 const browser=await chromium.launch({...(process.env.UI_BROWSER_EXECUTABLE?{executablePath:process.env.UI_BROWSER_EXECUTABLE}:{channel:'msedge'}),headless:true});
 await mkdir(output,{recursive:true});
 try{
  for(const width of [390,850,1280]){
   const page=await browser.newPage({viewport:{width,height:1000},serviceWorkers:'block'});
   const errors=[],external=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>{const u=new URL(r.request().url());if(u.origin===origin&&!u.pathname.startsWith('/api/'))return r.continue();external.push(u.origin);return r.abort();});
   await page.goto(`${origin}/tests/ui/news-registered-only.html`);
   await page.getByRole('heading',{name:'찾은 소식 2건',exact:true}).waitFor();
   const history=page.getByRole('button',{name:'이전 수집 기록 12개 보기',exact:true});await history.waitFor();
   assert.equal(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).isEnabled(),true);
   assert.equal(await page.getByText('등록 기관 사이트',{exact:true}).isVisible(),true);
   assert.doesNotMatch(await page.locator('main').innerText(),/복지로|600건|596건/);
   assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
   assert.ok((await page.getByRole('heading',{name:'찾은 소식 2건',exact:true}).boundingBox()).y<750);
   await page.screenshot({path:`${output}/news-restored-${width}.png`,fullPage:true});
   await history.click();
   assert.match(await page.locator('main').innerText(),/이전 수집 범위의 기록/);
   assert.equal(await page.getByRole('button',{name:'이 수집의 결과 보기',exact:true}).count(),12);
   assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
   await page.getByRole('button',{name:'이전 수집 기록 접기',exact:true}).click();
   assert.doesNotMatch(await page.locator('main').innerText(),/복지로/);
   await page.goto(`${origin}/tests/ui/news-registered-only.html?mode=partial`);
   await page.getByText('수집 완료 · 확인할 자료 있음',{exact:true}).waitFor();
   assert.equal(await page.getByRole('button',{name:'새 소식 찾기',exact:true}).isEnabled(),true);
   assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).count(),0);
   await page.getByText('처리 내역',{exact:true}).click();
   assert.match(await page.locator('main').innerText(),/시험 기관 A · 목록 확인 필요/);
   assert.match(await page.locator('main').innerText(),/개인정보 확인 전에는 화면을 AI로 보내지 않습니다/);
   await page.getByText('처리 내역',{exact:true}).click();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   assert.deepEqual(await page.evaluate(()=>window.fixtureCalls),[]);
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);await page.close();
  }
 }finally{await browser.close();}
});

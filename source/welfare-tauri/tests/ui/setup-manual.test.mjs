import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('매뉴얼 슬라이드 이동·확대·다운로드·작은 창을 검증한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:720,height:560}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
  for(const [topic,first] of [['google',11],['signup',15],['notion',18],['policy',20],['news',20],['policy-register',23],['news-register',23]]) {
   await page.goto(`http://127.0.0.1:1420/manual.html?topic=${topic}`);
   await page.locator(`img[src="/manual/page-${first}.webp"]`).waitFor();
   await page.waitForFunction(()=>document.querySelector('img')?.naturalWidth>0);
   assert.ok(await page.getByRole('button',{name:'이전 페이지',exact:true}).isDisabled());
   await page.getByRole('button',{name:'다음 페이지',exact:true}).click();
   await page.locator(`img[src="/manual/page-${first+1}.webp"]`).waitFor();
   await page.keyboard.press('ArrowLeft');
   await page.locator(`img[src="/manual/page-${first}.webp"]`).waitFor();
   await page.getByRole('button',{name:'확대 보기',exact:true}).click();
   assert.equal(await page.locator('.slide.zoomed').count(),1);
   await page.keyboard.press('Escape');
   assert.equal(await page.locator('.slide.zoomed').count(),0);
   assert.equal(await page.locator('select').count(),0);
   assert.equal(await page.getByRole('link',{name:'PDF 다운로드'}).getAttribute('href'),'/manual/welfare-f5-manual.pdf');
  }
  await page.goto('http://127.0.0.1:1420/manual.html?topic=google');
  await page.locator('img').waitFor();
  await page.screenshot({path:'../artifacts/setup-independent-manual.png'});
  await page.setViewportSize({width:420,height:360});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.ok(await page.locator('main').evaluate(e=>e.clientHeight>=160));
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
});

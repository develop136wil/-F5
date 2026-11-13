import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('공식 복제 영상은 기본 펼침·수동 재생이며 실패 후 재시도하거나 접을 수 있다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const page=await browser.newPage();let requests=0;const held=[];
  await page.route('**/*',route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='videos.ctfassets.net'){requests++;held.push(route);return;}
   return u.origin==='http://127.0.0.1:1420'?route.continue():route.abort();
  });
  await page.goto('http://127.0.0.1:1420/tests/ui/tutorial-media.html');
  await page.locator('video').waitFor();
  assert.equal(await page.getByRole('button',{name:'영상 접기',exact:true}).getAttribute('aria-expanded'),'true');
  assert.deepEqual(await page.locator('video').evaluate(el=>({autoplay:el.autoplay,controls:el.controls,paused:el.paused})),{autoplay:false,controls:true,paused:true});
  await page.locator('video').evaluate(el=>el.dispatchEvent(new Event('error')));
  for(const route of held.splice(0))await route.abort();
  await page.getByRole('status').waitFor();
  assert.equal(await page.getByRole('link',{name:'Notion 공식 안내',exact:false}).getAttribute('href'),'https://www.notion.com/ko/help/duplicate-public-pages');
  await page.getByRole('button',{name:'영상 다시 불러오기',exact:true}).click();await page.locator('video').waitFor();
  await page.getByRole('button',{name:'영상 접기',exact:true}).click();assert.equal(await page.locator('video').count(),0);
  for(const route of held.splice(0))await route.abort();
  assert.equal(await page.getByRole('button',{name:'영상 펼치기',exact:true}).getAttribute('aria-expanded'),'false');
 }finally{await browser.close();}
});

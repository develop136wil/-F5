import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('기존 단계 디자인으로 대상을 저장하며 수동 출처 UI를 표시하지 않는다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1000,height:950}});
  await page.route('**/*',r=>{
   const url=new URL(r.request().url());
   if(url.pathname.endsWith('/news/profile-taxonomy'))return r.fulfill({json:{life_cycles:['영유아','노년'],household_situations:['저소득','장애인'],interest_topics:['주거','생활지원']}});
   return url.origin==='http://127.0.0.1:1420'&&!url.pathname.startsWith('/api/')?r.continue():r.abort();
  });
  await page.goto('http://127.0.0.1:1420/tests/ui/condition-wizard.html');
  assert.equal(await page.getByText(/복지로/).count(),0);
  await page.getByRole('button',{name:'다음',exact:true}).click();
  await page.getByRole('button',{name:'노년',exact:true}).click();
  await page.getByRole('button',{name:'다음',exact:true}).click();
  await page.getByRole('button',{name:'다음',exact:true}).click();
  assert.equal(await page.getByText(/복지로|수집 출처|관심주제/).count(),0);
  await page.getByRole('button',{name:'최종 저장',exact:true}).click();
  await page.getByRole('alert').waitFor(); // fixture deliberately fails its first save
  const value=await page.evaluate(()=>window.saves[0]);
  assert.deepEqual(value.life_cycles,['노년']);
  await page.screenshot({path:'../artifacts/bokjiro-auto-preview/condition.png'});
  await page.setViewportSize({width:390,height:850});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }finally{await browser.close();}
});

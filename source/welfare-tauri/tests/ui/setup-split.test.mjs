import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('매뉴얼은 요청할 때만 별도 창으로 열리고 닫아도 튜토리얼이 유지된다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
  const context=await browser.newContext({viewport:{width:1540,height:1000}});
  const page=await context.newPage();
  await context.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
  await page.goto('http://127.0.0.1:1420/tests/ui/settings-drafts.html');
  await page.getByRole('link',{name:'정책 단계 시험',exact:true}).click();
  await page.getByRole('heading',{name:'AI와 복지F5 연결',exact:true}).waitFor();
  assert.equal(context.pages().length,1);
  assert.equal(await page.locator('iframe').count(),0);
  assert.ok((await page.locator('dialog').boundingBox()).width<=900);
  await page.getByRole('button',{name:/2\. Notion 가입/}).click();
  await page.getByRole('heading',{name:'3. 사용할 작업 공간 확인',exact:true}).waitFor();
  const opened=context.waitForEvent('page');
  await page.getByRole('button',{name:'설정 매뉴얼 열기'}).click();
  const manual=await opened;
  await manual.getByRole('img',{name:'Notion 가입 · PDF 15쪽'}).waitFor();
  const closed=manual.waitForEvent('close');
  await manual.getByRole('button',{name:'닫기',exact:true}).click();
  await closed;
  assert.ok(await page.locator('dialog').isVisible());
  await page.screenshot({path:'../artifacts/setup-independent-tutorial.png'});
  await page.getByRole('button',{name:'닫기',exact:true}).click();
  await page.getByText('모두 연결',{exact:true}).click();
  await page.getByRole('link',{name:'정책 단계 시험',exact:true}).click();
  await page.getByRole('heading',{name:'AI와 복지F5 연결',exact:true}).waitFor();
  assert.equal(context.pages().length,1);
  await page.screenshot({path:'../artifacts/setup-independent-complete.png'});
 } finally {await browser.close();}
});

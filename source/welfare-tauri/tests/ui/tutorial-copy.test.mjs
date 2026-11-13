import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
test('튜토리얼은 간결한 링크와 복제 후 권한 추가를 양쪽 템플릿에 안내한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:850,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
  const open=async query=>page.goto(`http://127.0.0.1:1420/tests/ui/setup-manual-links.html?${query}`);
  await open('kind=google');
  assert.equal(await page.getByRole('link',{name:'Google AI Studio 열기 — 새 창으로 열림',exact:true}).getAttribute('href'),'https://aistudio.google.com/api-keys');
  await open('kind=notion&phase=signup');
  await page.getByText(/매뉴얼은 무료 요금제를 선택하는 예시/).waitFor();
  await open('kind=notion');
  await page.getByText(/토큰 저장만으로는 정책표나 보관함에 접근할 수 없습니다/).waitFor();
  for(const kind of ['policy','news']){
   await open(`kind=${kind}&phase=template`);
   await page.getByRole('heading',{name:/복제한 .*연결 권한 추가하기/}).waitFor();
   await page.getByText(/권한을 확인한 뒤 페이지에 추가를 눌러 마칩니다/).waitFor();
   await page.setViewportSize({width:390,height:850});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

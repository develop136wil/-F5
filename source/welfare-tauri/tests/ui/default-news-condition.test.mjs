import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');

test('처음부터 지역·카테고리·나이를 저장하고 실행 준비와 구별한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:850,height:650}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>new URL(r.request().url()).origin==='http://127.0.0.1:1420'&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
  await page.goto('http://127.0.0.1:1420/tests/ui/settings-drafts.html');
  await page.getByRole('link',{name:'수집 조건 단계 시험',exact:true}).click();
  const dialog=page.getByRole('dialog');
  const wizard=dialog.getByRole('region',{name:'새 수집 조건 만들기'});
  await wizard.getByLabel('시·도',{exact:true}).selectOption('서울특별시');
  await wizard.getByLabel('시·군·구 (선택)',{exact:true}).fill('도봉구');
  await wizard.getByRole('button',{name:'다음',exact:true}).click();
  await wizard.getByRole('button',{name:'다문화',exact:true}).click();
  assert.equal(await wizard.getByRole('button',{name:'일반',exact:true}).count(),0);
  assert.equal(await wizard.getByRole('button',{name:'기타',exact:true}).count(),0);
  await mkdir('../artifacts/default-news-condition',{recursive:true});
  await page.screenshot({path:'../artifacts/default-news-condition/categories-850.png'});
  await page.keyboard.press('Escape');
  await dialog.getByText('아직 저장하지 않은 입력이 있습니다.',{exact:false}).waitFor();
  await dialog.getByRole('button',{name:'계속 설정',exact:true}).click();
  await wizard.getByRole('button',{name:'다음',exact:true}).click();
  await wizard.getByRole('button',{name:'다음',exact:true}).click();
  assert.equal(await page.evaluate(()=>globalThis.__fixtureReadDefaultCondition()),null);
  await wizard.getByRole('button',{name:'최종 저장',exact:true}).click();
  await dialog.getByRole('heading',{name:'사이트',exact:true}).waitFor();
  const saved=await page.evaluate(()=>globalThis.__fixtureReadDefaultCondition());
  assert.deepEqual(saved.characteristics,['다문화']);assert.equal(saved.region,'서울특별시 도봉구');
  assert.equal(saved.min_age_months,null);assert.equal(saved.max_age_months,null);
  assert.equal(await dialog.evaluate(el=>el.scrollWidth>el.clientWidth),false);
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});

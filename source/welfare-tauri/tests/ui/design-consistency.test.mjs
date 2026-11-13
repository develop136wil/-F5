import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
async function open(browser,path,width=1280){
 const page=await browser.newPage({viewport:{width,height:900}});
 page.errors=[];page.on('pageerror',error=>page.errors.push(error.message));
 await page.route('**/*',r=>new URL(r.request().url()).origin===origin&&!new URL(r.request().url()).pathname.startsWith('/api/')?r.continue():r.abort());
 await page.goto(`${origin}/tests/ui/${path}`);return page;
}
async function openWordEditors(scope){
 for(const label of ['찾을 단어','제외할 단어']) {
  const summary=scope.locator('summary').filter({hasText:`${label} 한꺼번에 편집`});
  if(await summary.evaluate(el=>!el.parentElement.open))await summary.click();
 }
}
test('설정 단계는 상태·형태로 구분하며 좁은 화면과 다크 모드에서도 세부 기능을 발견할 수 있다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await open(browser,'condition-wizard.html');
  await mkdir('../artifacts/design-consistency',{recursive:true});
  const steps=page.getByRole('list',{name:'수집 조건 설정 단계'});
  assert.match((await steps.locator('[aria-current="step"]').innerText()).replace(/\s+/g,' '),/1단계 · 현재 · 지역/);
  for(const width of [1280,850,390])for(const dark of [false,true]){
   await page.setViewportSize({width,height:900});
   await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
   await page.getByLabel('시·도',{exact:true}).waitFor();
   assert.ok((await page.getByLabel('시·도',{exact:true}).boundingBox()).width<=768);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:`../artifacts/design-consistency/region-${width}-${dark?'dark':'light'}.png`,fullPage:true});
  }
  await page.locator('summary').filter({hasText:'수집 세부 설정'}).focus();await page.keyboard.press('Enter');
  await openWordEditors(page);
  await page.getByRole('textbox',{name:'찾을 단어',exact:true}).fill('돌봄, 부모 교육\n돌봄');
  await page.getByRole('textbox',{name:'제외할 단어',exact:true}).fill('채용, 입찰');
  await page.getByLabel('AI 요청',{exact:true}).fill('신청 방법을 쉽게 설명해주세요.');
  await page.getByRole('button',{name:'다음',exact:true}).click();
  assert.equal(await steps.locator('li').first().getByText('1단계 · 완료 ·',{exact:false}).count(),1);
  assert.equal(await steps.locator('li').first().locator('svg').count(),1);
  await page.getByRole('button',{name:'다음',exact:true}).click();await page.getByRole('button',{name:'다음',exact:true}).click();
  assert.equal(await page.evaluate(()=>window.saves.length),0);
  await page.getByRole('button',{name:'최종 저장',exact:true}).click();await page.getByRole('alert').waitFor();
  assert.equal(await page.getByLabel('AI 요청',{exact:true}).inputValue(),'신청 방법을 쉽게 설명해주세요.');
  await page.getByRole('button',{name:'최종 저장',exact:true}).click();
  await page.waitForFunction(()=>window.saves.length===2);
  const saves=await page.evaluate(()=>window.saves);assert.deepEqual(saves[0],saves[1]);
  assert.deepEqual(saves[1].keywords,['돌봄','부모 교육']);assert.deepEqual(saves[1].excluded_keywords,['채용','입찰']);
  assert.deepEqual(page.errors,[]);
 }finally{await browser.close();}
});
test('현재 조건의 세부 설정 저장·재열기·취소·충돌에서 기존 설정과 대상 범위를 보존한다',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await open(browser,'settings-drafts.html?mode=conditions');
  await page.getByRole('tab',{name:'기능 설정',exact:true}).click();
  const section=page.getByRole('region',{name:'새 소식 수집 조건',exact:true});
  const edit=()=>section.getByRole('button',{name:/^수집 세부 설정/});
  await edit().waitFor();
  const before=await page.evaluate(()=>({profile:window.__fixtureReadDefaultCondition(),config:window.__fixtureReadConfig()}));
  await edit().click();
  await openWordEditors(section);
  await section.getByRole('textbox',{name:'찾을 단어',exact:true}).fill('돌봄, 부모 교육');
  await section.getByRole('textbox',{name:'제외할 단어',exact:true}).fill('채용');
  await section.getByLabel('AI 요청',{exact:true}).fill('신청 대상을 쉽게 설명해주세요.');
  assert.deepEqual(await page.evaluate(()=>window.__fixtureReadDefaultCondition()),before.profile);
  await section.getByRole('button',{name:'최종 저장',exact:true}).click();await edit().waitFor();
  const saved=await page.evaluate(()=>window.__fixtureReadDefaultCondition());
  assert.deepEqual(saved.keywords,['돌봄','부모 교육']);assert.deepEqual(saved.excluded_keywords,['채용']);assert.equal(saved.ai_prompt,'신청 대상을 쉽게 설명해주세요.');
  for(const key of ['region','min_age_months','max_age_months','life_cycles','site_ids'])assert.deepEqual(saved[key],before.profile[key]);
  assert.deepEqual(await page.evaluate(()=>window.__fixtureReadConfig()),before.config);
  await edit().click();await openWordEditors(section);assert.equal(await section.getByRole('textbox',{name:'제외할 단어',exact:true}).inputValue(),'채용');
  await section.getByRole('textbox',{name:'제외할 단어',exact:true}).fill('취소할 입력');
  await section.getByRole('button',{name:'취소',exact:true}).click();await edit().click();
  await openWordEditors(section);
  assert.equal(await section.getByRole('textbox',{name:'제외할 단어',exact:true}).inputValue(),'채용');
  await page.evaluate(()=>window.__fixtureBumpDefaultRevision());
  await section.getByLabel('AI 요청',{exact:true}).fill('충돌 후에도 남아야 할 초안');
  await section.getByRole('button',{name:'최종 저장',exact:true}).click();await section.getByRole('alert').waitFor();
  assert.equal(await section.getByLabel('AI 요청',{exact:true}).inputValue(),'충돌 후에도 남아야 할 초안');
  assert.equal((await page.evaluate(()=>window.__fixtureReadDefaultCondition())).ai_prompt,saved.ai_prompt);
  await section.getByRole('button',{name:'취소',exact:true}).click();
  const legacy=page.locator('summary').filter({hasText:'이전 수집 설정'});
  assert.equal(await legacy.count(),0);await page.getByRole('button',{name:'저장한 다른 조건',exact:true}).click();assert.equal(await legacy.count(),0);
  assert.deepEqual(page.errors,[]);
 }finally{await browser.close();}
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin='http://127.0.0.1:1420';
async function setup(t,query){
 const browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage({viewport:{width:1100,height:900}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',r=>{const u=new URL(r.request().url());return u.origin===origin&&!u.pathname.startsWith('/api/')?r.continue():r.abort();});
 t.after(async()=>{assert.deepEqual(errors,[]);assert.deepEqual(await page.evaluate(()=>functionFixture.unexpected),[]);await browser.close();});
 await page.goto(`${origin}/tests/ui/function-errors.html?${query}`);await page.getByRole('heading',{level:1}).waitFor();return page;
}
test('리포트 조회 실패는 빈 데이터가 아니며 재시도 후 실제 집계와 빈 결과를 구별한다',async t=>{
 const page=await setup(t,'page=reports&fail');
 for(const label of ['사이트 필터','수집 통계','수집 추이'])await page.getByText(`${label} 조회에 실패했습니다.`,{exact:true}).waitFor();
 await mkdir('../artifacts/finalist-fixes-20260908',{recursive:true});
 await page.screenshot({path:'../artifacts/finalist-fixes-20260908/reports-query-failure.png',fullPage:true});
 assert.equal(await page.getByText('데이터 없음',{exact:true}).count(),0);assert.equal(await page.getByText('수집 데이터가 없습니다.',{exact:true}).count(),0);
 await page.getByLabel('사이트',{exact:true}).isDisabled().then(v=>assert.equal(v,true));
 await page.evaluate(()=>{functionFixture.fail={sites:false,stats:false,trends:false};functionFixture.stats={total:0,by_category:{},by_site:{}};functionFixture.trends=[];});
 for(const label of ['사이트 필터','수집 통계','수집 추이'])await page.getByRole('button',{name:`${label} 다시 시도`,exact:true}).click();
 await page.getByText('수집 데이터가 없습니다.',{exact:true}).waitFor();assert.equal(await page.getByText('데이터 없음',{exact:true}).count(),2);
 assert.equal(await page.getByText(/조회에 실패했습니다/).count(),0);
});
test('리포트 새로고침 실패는 이전 통계를 유지하고 이전 자료임을 표시한다',async t=>{
 const page=await setup(t,'page=reports');
 await page.waitForFunction(()=>functionFixture.client.getQueryData(functionFixture.queryKeys.summaryStats)?.total===1);
 await page.evaluate(async()=>{functionFixture.fail.stats=true;await functionFixture.client.invalidateQueries({queryKey:functionFixture.queryKeys.summaryStats});});
 await page.getByText('수집 통계 조회에 실패했습니다.',{exact:true}).waitFor();await page.getByText('이전에 불러온 자료를 표시하고 있습니다.',{exact:false}).waitFor();
 assert.equal(await page.getByText('데이터 없음',{exact:true}).count(),0);
 assert.equal(await page.evaluate(()=>functionFixture.client.getQueryData(functionFixture.queryKeys.summaryStats).total),1);
});
test('사이트 조회 실패는 0개·설정 완료로 표시하지 않고 재시도 뒤 정상 목록을 보여준다',async t=>{
 const page=await setup(t,'page=sites&fail');await page.getByText('사이트 목록 조회에 실패했습니다.',{exact:true}).waitFor();
 await mkdir('../artifacts/finalist-fixes-20260908',{recursive:true});
 await page.screenshot({path:'../artifacts/finalist-fixes-20260908/sites-query-failure.png',fullPage:true});
 assert.equal(await page.getByRole('heading',{name:'등록된 사이트 (0)',exact:true}).count(),0);assert.equal(await page.getByText('등록된 사이트가 없습니다.',{exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'주소 등록',exact:true}).isDisabled(),true);
 const input=page.getByRole('textbox',{name:'공지사항·새 소식 게시판 주소',exact:true});await input.fill('https://example.invalid/draft');
 await page.evaluate(()=>functionFixture.fail.sites=false);await page.getByRole('button',{name:'사이트 목록 다시 시도',exact:true}).click();
 await page.getByRole('heading',{name:'시험 복지 기관',exact:true}).waitFor();assert.equal(await input.inputValue(),'https://example.invalid/draft');
});
test('사이트 수집 상태와 실행 이력의 조회 실패도 없던 기능·빈 이력으로 숨기지 않는다',async t=>{
 const page=await setup(t,'page=sites');await page.getByRole('heading',{name:'시험 복지 기관',exact:true}).waitFor();
 await page.evaluate(async()=>{functionFixture.fail.health=true;await functionFixture.client.invalidateQueries({queryKey:functionFixture.queryKeys.sitesHealth});});
 await page.getByText('사이트 수집 상태 조회에 실패했습니다.',{exact:true}).waitFor();
 await page.evaluate(()=>{functionFixture.fail.health=false;functionFixture.fail.history=true;functionFixture.health=[{site_id:'fixture',site_name:'시험 복지 기관',status:'healthy',total_runs_30d:1,total_items_found:1,success_rate:100}];});
 await page.getByRole('button',{name:'사이트 수집 상태 다시 시도',exact:true}).click();
 await page.getByRole('button',{name:/정상 시험 복지 기관/}).click();
 await page.getByText('사이트 실행 이력 조회에 실패했습니다.',{exact:true}).waitFor();assert.equal(await page.getByText('실행 이력 없음',{exact:true}).count(),0);
 await page.evaluate(()=>functionFixture.fail.history=false);await page.getByRole('button',{name:'사이트 실행 이력 다시 시도',exact:true}).click();await page.getByText('실행 이력 없음',{exact:true}).waitFor();
});
for(const [button,method] of [['주소 등록','addSitesBatch']])test(`${button}은 늦은 성공에 새 초안을 지우지 않고 중복 등록을 차단한다`,async t=>{
 const page=await setup(t,'page=sites');const input=page.getByRole('textbox',{name:'공지사항·새 소식 게시판 주소',exact:true});
 await page.getByRole('heading',{name:'시험 복지 기관',exact:true}).waitFor();await input.fill('https://example.invalid/submitted');await page.getByRole('button',{name:button,exact:true}).click();
 await page.waitForFunction(()=>functionFixture.pending.length===1);
 assert.equal(await page.locator('form button').evaluateAll(els=>els.every(e=>e.disabled)),true);
 // Even a same-value edit is a new user draft, not the submitted snapshot.
 await input.fill('https://example.invalid/new-draft');await input.fill('https://example.invalid/submitted');
 await page.evaluate(()=>functionFixture.pending.shift().resolve({count:1}));await page.getByRole('button',{name:button,exact:true}).waitFor();
 assert.equal(await input.inputValue(),'https://example.invalid/submitted');
 await page.waitForFunction(()=>!document.querySelector('form button').disabled);
 assert.equal(await page.evaluate(name=>functionFixture.calls.filter(x=>x.name===name).length,method),1);
 await page.getByRole('button',{name:button,exact:true}).click();await page.waitForFunction(()=>functionFixture.pending.length===1);
 await page.evaluate(()=>functionFixture.pending.shift().reject(Error('Synthetic failure')));await page.getByRole('button',{name:button,exact:true}).waitFor();
 assert.equal(await input.inputValue(),'https://example.invalid/submitted');
 await page.waitForFunction(()=>!document.querySelector('form button').disabled);await page.getByRole('button',{name:button,exact:true}).click();await page.waitForFunction(()=>functionFixture.pending.length===1);
 await page.evaluate(()=>functionFixture.pending.shift().resolve({count:1}));await page.waitForFunction(()=>document.querySelector('textarea').value==='');
});

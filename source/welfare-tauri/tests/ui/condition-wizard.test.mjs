import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
test('최종 저장 전 쓰기 없음, 실패 후 조건 유지, 중복 클릭 및 취소 검증',async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try {
 const page=await browser.newPage();
 await page.route('**/*',r=>new URL(r.request().url()).origin==='http://127.0.0.1:1420'?r.continue():r.abort());
 await page.goto('http://127.0.0.1:1420/tests/ui/condition-wizard.html');
 await page.getByLabel('시·도',{exact:true}).selectOption('서울특별시');
 await page.getByLabel('시·군·구 (선택)',{exact:true}).fill('도봉구');
 await page.getByRole('button',{name:'다음',exact:true}).click();
 await page.getByRole('button',{name:'다문화',exact:true}).click();
 await page.getByRole('button',{name:'다음',exact:true}).click();
 await page.getByRole('button',{name:'다음',exact:true}).click();
 assert.equal(await page.evaluate(()=>window.saves.length),0);
 await page.getByRole('button',{name:'최종 저장',exact:true}).evaluate(el=>{el.click();el.click();});
 await page.getByRole('alert').waitFor();
 assert.equal(await page.evaluate(()=>window.saves.length),1);
 await page.getByRole('button',{name:'최종 저장',exact:true}).click();
 await page.getByRole('button',{name:'최종 저장',exact:true}).waitFor();
 const saves=await page.evaluate(()=>window.saves);
 assert.equal(saves.length,2);assert.deepEqual(saves[0],saves[1]);
 assert.deepEqual(saves[0].characteristics,['다문화']);assert.equal(saves[0].min_age_months,null);
 assert.equal(saves[0].region,'서울특별시 도봉구');
 await page.reload();await page.getByRole('button',{name:'취소',exact:true}).click();
 assert.equal(await page.evaluate(()=>window.cancelCount),1);assert.equal(await page.evaluate(()=>window.saves.length),0);
 }finally{await browser.close();}
});

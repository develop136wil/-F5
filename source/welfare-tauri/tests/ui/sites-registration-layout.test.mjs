import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1423';

test('게시판 주소 입력과 등록 버튼은 같은 높이이고 안내는 간결하다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        await mkdir('../artifacts/news-ui-polish-20260917',{recursive:true});
        for(const width of [1280,850,425]) {
            const page=await browser.newPage({viewport:{width,height:900},serviceWorkers:'block'});
            const errors=[];page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*',route=>{const url=new URL(route.request().url());return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();});
            await page.goto(origin+'/tests/ui/accessibility-reflow.html?page=sites');
            const input=page.getByLabel('공지사항·새 소식 게시판 주소',{exact:true});
            const submit=page.getByRole('button',{name:'주소 등록',exact:true});
            await input.waitFor();
            const [field,button]=await Promise.all([input.boundingBox(),submit.boundingBox()]);
            if(width>=640){assert.ok(Math.abs(field.y-button.y)<1);assert.ok(Math.abs(field.height-button.height)<1);}
            else assert.ok(button.y>=field.y+field.height);
            await page.getByText('공지사항·모집 안내 게시판의 목록 주소를 입력하세요. 여러 주소는 한 줄에 하나씩 입력하세요.',{exact:true}).waitFor();
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            const fixture=await page.evaluate(()=>window.a11yFixture);
            assert.deepEqual(fixture.unexpected,[]);assert.deepEqual(fixture.writes,[]);assert.deepEqual(errors,[]);
            await page.screenshot({path:`../artifacts/news-ui-polish-20260917/sites-registration-${width}.png`});
            await page.close();
        }
    } finally {await browser.close();}
});

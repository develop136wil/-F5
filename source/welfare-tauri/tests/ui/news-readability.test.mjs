import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';

test('새 소식은 제목을 먼저 보여주고 확인 사유는 펼치되 저장 확인은 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        await mkdir('../artifacts/news-readability',{recursive:true});
        for(const width of [582,850,1280]) for(const dark of [false,true]) {
            const page=await browser.newPage({viewport:{width,height:900}});
            const errors=[];page.on('pageerror',error=>errors.push(error.message));
            await page.route('**/*',route=>new URL(route.request().url()).origin===origin&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
            await page.goto(origin+'/tests/ui/news-actions.html');
            await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            const card=page.getByRole('article').filter({has:page.getByRole('button',{name:'시험용 한부모 가족 지원 전체 내용 미리보기',exact:true})});
            await card.waitFor();
            const summary=card.locator('summary');
            const note=card.getByText('페이지 용량 제한으로 일부를 읽지 못했습니다.',{exact:true});
            assert.equal(await note.isVisible(),false);
            assert.ok((await card.getByText('시험용 한부모 가족 지원',{exact:true}).boundingBox()).y < (await summary.boundingBox()).y);
            await summary.focus();await page.keyboard.press('Enter');
            assert.equal(await note.isVisible(),true);
            assert.equal(await page.getByRole('dialog').count(),0);
            await summary.click();
            assert.equal(await note.isVisible(),false);
            await page.getByRole('button',{name:'모든 전송 확인 완료',exact:true}).click();
            assert.equal(await page.getByRole('button',{name:'Notion 확인 후 재시도 허용',exact:true}).count(),0);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await page.locator('main > div').filter({has:page.getByRole('heading',{name:'새 소식',exact:true})}).screenshot({path:`../artifacts/news-readability/news-${dark?'dark':'light'}-${width}.png`});
            await card.getByRole('checkbox').check();
            await page.getByRole('button',{name:'소식 모음 파일 저장 (1)',exact:true}).click();
            await page.getByRole('dialog').waitFor();
            assert.match(await page.getByRole('dialog').innerText(),/원문|확인/);
            await page.getByRole('button',{name:'취소',exact:true}).click();
            assert.match(await page.locator('output').innerText(),/"html":0/);
            assert.deepEqual(errors,[]);
            await page.close();
        }
    }finally{await browser.close();}
});

test('과거 완료·취소 기록은 접고 대기 작업과 재개 버튼은 계속 보인다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:850,height:900}});
        await page.route('**/*',route=>new URL(route.request().url()).origin===origin&&!new URL(route.request().url()).pathname.startsWith('/api/')?route.continue():route.abort());
        await page.goto(origin+'/tests/ui/news-collection.html?mode=history');
        const show=page.getByText('수집 이력',{exact:true});
        await show.waitFor();
        const history=show.locator('..');
        assert.equal(await history.getAttribute('open'),null);
        assert.equal(await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).count(),1);
        assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).isVisible(),true);
        await show.click();
        assert.equal(await history.getByText('상세 보기',{exact:true}).count(),3);
        assert.equal(await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).count(),1,'개별 상세는 기본 접힘');
        for(const detail of await history.getByText('상세 보기',{exact:true}).all())await detail.click();
        assert.equal(await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).count(),4);
        await show.click();
        assert.equal(await history.getAttribute('open'),null);
        assert.equal(await page.getByRole('button',{name:'찾은 소식 보기',exact:true}).count(),1);
        assert.equal(await page.getByRole('button',{name:'이어서 진행',exact:true}).isVisible(),true);
        assert.deepEqual(await page.evaluate(()=>window.starts),[]);
        assert.deepEqual(await page.evaluate(()=>window.resumes),[]);
        assert.deepEqual(await page.evaluate(()=>window.cancels),[]);
    }finally{await browser.close();}
});

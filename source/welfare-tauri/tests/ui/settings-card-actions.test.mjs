import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1427';

test('설정 카드 주요 버튼은 폭·높이를 맞추고 연결 해제는 작게 유지한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:900,height:1100}});
        const errors=[],blocked=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>{
            const url=new URL(route.request().url());
            if(url.origin!==origin || url.pathname.startsWith('/api/')){blocked.push(url.href);return route.abort();}
            return route.continue();
        });
        await page.goto(`${origin}/tests/ui/settings-drafts.html`);
        await page.getByRole('button',{name:'모두 연결',exact:true}).click();
        const token=page.getByRole('link',{name:'Notion 토큰 확인·변경',exact:true});
        const storage=page.getByRole('link',{name:'보관함 연결·변경',exact:true});
        const policy=page.getByRole('link',{name:'정책표 연결·변경',exact:true});
        const disconnect=page.getByRole('button',{name:'새 소식 보관함 연결 해제',exact:true});
        await token.waitFor();
        assert.equal(await token.getAttribute('href'),'/settings?tab=guide&step=policy&edit=notion');
        assert.equal(await storage.getAttribute('href'),'/settings?tab=guide&step=news&edit=news');
        assert.equal(await policy.getAttribute('href'),'/settings?tab=guide&step=policy&edit=policy');
        await mkdir('../artifacts/settings-card-actions-20260911',{recursive:true});
        for(const width of [900,390])for(const dark of [false,true]){
            await page.setViewportSize({width,height:1100});
            await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            const a=await token.boundingBox(),b=await storage.boundingBox(),c=await policy.boundingBox(),d=await disconnect.boundingBox();
            assert.ok(Math.abs(a.width-b.width)<1);
            assert.ok(a.height>=56 && b.height>=56 && c.height>=56);
            assert.ok(d.height>=44 && d.height<56,'해제 버튼 크기 유지');
            if(width===900){assert.ok(Math.abs(a.y-b.y)<1);assert.ok(c.width>a.width*1.9);assert.ok(d.width<a.width);}
            else{assert.ok(b.y>a.y);assert.ok(Math.abs(a.width-c.width)<1);}
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await page.getByRole('heading',{name:'Notion 연결',exact:true}).locator('..').locator('..').screenshot({path:`../artifacts/settings-card-actions-20260911/notion-${width}-${dark?'dark':'light'}.png`});
        }
        await token.focus();
        assert.equal(await token.evaluate(el=>el===document.activeElement),true);
        await token.press('Enter');
        await page.getByRole('dialog',{name:'연결·설정 도우미'}).waitFor();
        await page.getByRole('dialog').getByRole('button',{name:'닫기',exact:true}).click();
        await page.getByRole('tab',{name:'시작 안내',exact:true}).click();
        const group=page.getByRole('group',{name:'시작 안내 선택'});
        const links=group.getByRole('link');
        for(const width of [900,390])for(const dark of [false,true]){
            await page.setViewportSize({width,height:1100});
            await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            const boxes=await Promise.all([0,1,2].map(i=>links.nth(i).boundingBox()));
            assert.ok(boxes.every(box=>box.height>=64));
            assert.ok(boxes.every(box=>Math.abs(box.width-boxes[0].width)<1));
            assert.ok(boxes.every(box=>Math.abs(box.height-boxes[0].height)<1));
            if(width===900)assert.ok(boxes.every(box=>Math.abs(box.y-boxes[0].y)<1));
            else assert.ok(boxes[2].y>boxes[1].y && boxes[1].y>boxes[0].y);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await group.locator('..').screenshot({path:`../artifacts/settings-card-actions-20260911/guide-${width}-${dark?'dark':'light'}.png`});
        }
        assert.deepEqual(await links.evaluateAll(nodes=>nodes.map(node=>node.getAttribute('href'))),[
            '/settings?tab=guide&step=policy','/settings?tab=guide&step=google','/settings?tab=guide&step=news',
        ]);
        assert.deepEqual(errors,[]);
        assert.deepEqual(blocked,[],'실제 API나 외부 서비스 호출 없음');
    }finally{await browser.close();}
});

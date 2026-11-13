import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';

test('실제 수집 세부 설정의 포함·제외 태그와 AI 요청이 서로 독립적으로 저장된다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:850,height:1100}});
        const errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>{
            const url=new URL(route.request().url());
            return url.origin===origin && !url.pathname.startsWith('/api/') ? route.continue() : route.abort();
        });
        await page.goto(`${origin}/tests/ui/keyword-details.html`);
        const includes=page.getByRole('list',{name:'찾을 단어 목록'});
        const excludes=page.getByRole('list',{name:'제외할 단어 목록'});
        await includes.waitFor();
        assert.equal(await includes.getByRole('listitem').count(),8);
        assert.equal(await excludes.getByRole('listitem').count(),8);
        await mkdir('../artifacts/keyword-tags-20260911',{recursive:true});
        for(const width of [390,850])for(const dark of [false,true]){
            await page.setViewportSize({width,height:1100});
            await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await page.screenshot({path:`../artifacts/keyword-tags-20260911/details-${width}-${dark?'dark':'light'}.png`,fullPage:true});
        }
        await includes.getByRole('button',{name:'찾을 단어: 돌봄 삭제',exact:true}).click();
        const entry=page.getByLabel('제외할 단어 추가 입력',{exact:true});
        await entry.fill('시험 제외');await entry.press('Enter');
        await page.getByLabel('AI 요청',{exact:true}).fill('변경한 시험 요청');
        await page.getByRole('button',{name:'최종 저장',exact:true}).click();
        const saved=await page.evaluate(()=>window.detailsSaved);
        assert.equal(saved.keywords.length,21);
        assert.equal(saved.excluded_keywords.length,74);
        assert.equal(saved.keywords.includes('돌봄'),false);
        assert.equal(saved.excluded_keywords.at(-1),'시험 제외');
        assert.equal(saved.ai_prompt,'변경한 시험 요청');
        assert.deepEqual(errors,[]);
    }finally{await browser.close();}
});

test('기존 단어 추가·삭제·되돌리기·검색·일괄 편집과 저장 전 입력 보존',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:850,height:1000}});
        const errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
        await page.goto(`${origin}/tests/ui/keyword-list.html`);
        const entry=page.getByLabel('찾을 단어 추가 입력',{exact:true});
        const list=page.getByRole('list',{name:'찾을 단어 목록'});
        await entry.waitFor();assert.equal(await list.getByRole('listitem').count(),8);
        assert.equal((await page.evaluate(()=>window.keywordDraft)).length,73);
        await page.getByRole('button',{name:'전체 73개 보기',exact:true}).click();
        assert.equal(await list.getByRole('listitem').count(),73);
        await entry.fill('돌봄, 새 지원, 새 지원');await entry.press('Enter');
        assert.equal(await list.getByRole('listitem').count(),74);
        assert.equal(await entry.inputValue(),'');assert.equal(await page.evaluate(()=>window.keywordSaved),null);
        await entry.fill('새 지원');await page.getByRole('button',{name:'찾을 단어 추가',exact:true}).click();
        await page.getByText('이미 등록된 단어입니다.',{exact:false}).waitFor();
        assert.equal(await list.getByRole('listitem').count(),74);
        await list.getByRole('button',{name:'찾을 단어: 부모 교육 삭제',exact:true}).click();
        assert.equal(await list.getByRole('listitem').count(),73);
        assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'찾을 단어: 시험 단어 1 삭제');
        await page.getByRole('button',{name:'삭제 되돌리기',exact:true}).click();
        assert.equal(await page.evaluate(()=>window.keywordDraft[1]),'부모 교육');
        const filter=page.getByLabel('찾을 단어 목록에서 찾기',{exact:true});
        await filter.fill('새 지원');assert.equal(await list.getByRole('listitem').count(),1);
        assert.equal((await page.evaluate(()=>window.keywordDraft)).length,74);await filter.fill('');
        await entry.fill('조합 중');
        await entry.evaluate(el=>el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true})));
        await entry.press('Enter');assert.equal(await entry.inputValue(),'조합 중');
        assert.equal((await page.evaluate(()=>window.keywordDraft)).length,74);
        await entry.evaluate(el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true})));
        await entry.press('Enter');assert.ok((await page.evaluate(()=>window.keywordDraft)).includes('조합 중'));
        await entry.fill('저장 직전 입력');await page.getByRole('button',{name:'최종 저장',exact:true}).click();
        assert.ok((await page.evaluate(()=>window.keywordSaved)).includes('저장 직전 입력'),'추가를 누르지 않아도 blur 후 최종 저장에 포함');
        await page.locator('summary').filter({hasText:'찾을 단어 한꺼번에 편집'}).click();
        await page.getByRole('textbox',{name:'찾을 단어',exact:true}).fill('하나,둘\n하나');
        assert.deepEqual(await page.evaluate(()=>window.keywordDraft),['하나','둘']);
        await page.getByRole('button',{name:'잠금 전환'}).click();assert.equal(await entry.isDisabled(),true);
        assert.equal(await list.getByRole('button').first().isDisabled(),true);
        await page.getByRole('button',{name:'잠금 전환'}).click();
        await page.getByRole('textbox',{name:'찾을 단어',exact:true}).fill(['긴단어'.repeat(45),...Array.from({length:72},(_,i)=>`복지 소식 ${i+1}`)].join('\n'));
        await page.locator('summary').filter({hasText:'찾을 단어 한꺼번에 편집'}).click();
        await mkdir('../artifacts/keyword-list',{recursive:true});
        for(const width of [390,850])for(const dark of [false,true]){
            await page.setViewportSize({width,height:1000});await page.evaluate(value=>document.documentElement.classList.toggle('dark',value),dark);
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            assert.equal(await list.evaluate(el=>el.scrollWidth>el.clientWidth),false);
            const box=await list.getByRole('button').first().boundingBox();assert.ok(box.width>=44&&box.height>=44);
            await page.screenshot({path:`../artifacts/keyword-list/${width}-${dark?'dark':'light'}.png`,fullPage:true});
        }
        assert.deepEqual(errors,[]);
    }finally{await browser.close();}
});

test('태그 접기는 저장값을 보존하고 숨은 단어 검색·여러 줄 붙여넣기를 지원한다',async()=>{
    const browser=await chromium.launch({channel:'msedge',headless:true});
    try{
        const page=await browser.newPage({viewport:{width:390,height:900}});
        const requests=[];
        await page.route('**/*',route=>{
            const url=new URL(route.request().url());
            if(url.origin!==origin || url.pathname.startsWith('/api/')){requests.push(url.href);return route.abort();}
            return route.continue();
        });
        await page.goto(`${origin}/tests/ui/keyword-list.html`);
        const entry=page.getByLabel('찾을 단어 추가 입력',{exact:true});
        const list=page.getByRole('list',{name:'찾을 단어 목록'});
        const filter=page.getByLabel('찾을 단어 목록에서 찾기',{exact:true});
        await entry.waitFor();
        assert.equal(await list.getByRole('listitem').count(),8);
        const toggle=page.getByRole('button',{name:'전체 73개 보기',exact:true});
        assert.equal(await toggle.getAttribute('aria-expanded'),'false');
        await filter.fill('시험 단어 71');
        assert.equal(await list.getByRole('listitem').count(),1,'접힌 목록 밖의 단어도 검색');
        await filter.fill('없는단어');
        await page.getByText('전체 73개 중 0개 표시',{exact:false}).waitFor();
        await filter.fill('');
        await entry.fill('바꿀 입력');
        await entry.evaluate(el=>{
            el.setSelectionRange(0,el.value.length);
            const clipboardData=new DataTransfer();clipboardData.setData('text/plain','추가 하나\r\n추가 둘,돌봄,추가 하나');
            el.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData}));
        });
        assert.deepEqual((await page.evaluate(()=>window.keywordDraft)).slice(-2),['추가 하나','추가 둘']);
        assert.equal((await page.evaluate(()=>window.keywordDraft)).length,75);
        assert.equal(await entry.inputValue(),'');
        assert.equal(await list.getByRole('listitem').count(),75,'추가한 태그가 보이도록 펼침');
        assert.equal(await list.evaluate(el=>el.scrollHeight>el.clientHeight),false,'작은 내부 스크롤 없이 펼침');
        await page.getByRole('button',{name:'간단히 보기',exact:true}).click();
        assert.equal(await list.getByRole('listitem').count(),8);
        await list.getByRole('button',{name:'찾을 단어: 돌봄 삭제',exact:true}).click();
        assert.equal(await list.getByRole('listitem').count(),8,'삭제 후 다음 태그로 미리보기 채움');
        await page.getByRole('button',{name:'삭제 되돌리기',exact:true}).click();
        await page.getByRole('button',{name:'최종 저장',exact:true}).click();
        assert.equal((await page.evaluate(()=>window.keywordSaved)).length,75,'접힌 태그도 모두 저장');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(requests,[],'운영 API·외부 요청 없이 동작');
    }finally{await browser.close();}
});

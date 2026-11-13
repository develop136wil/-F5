import {test,before,after} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
// Reuse one disposable browser process, but never a context or storage state.
// A launch failure still fails the suite; it is not retried or skipped.
let browser;
before(async()=>{browser=await chromium.launch({channel:'msedge',headless:true});});
after(async()=>{if(browser)await browser.close();});
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const artifactRoot=process.env.UI_TEST_ARTIFACT_DIR||'../artifacts';
const previewFor=url=>({url,db_key:'test',fields:{title:'모의 정책',category:'생활 지원',residence:['전국'],target_characteristics:['일반'],life_cycle:['전생애'],start_age_months:0,end_age_months:1800,age_detail:'나이 제한 없음',target_income:'소득 무관',eligibility_detail:'모의 조건',support_detail:'모의 지원 내용',contact:'모의 문의처'},options:{category:['생활 지원'],residence:['전국'],target_characteristics:['일반'],life_cycle:['전생애']}});
const retryStatus=url=>({state:'not_found',can_retry:true,attempt_id:'fixture-attempt',source_url:url,target_deeplink:'https://www.notion.so/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',target_name:'원래 시험 정책표',message:'이 출처의 등록 여부를 원래 정책표에서 직접 확인해주세요.'});
async function fixture(t,mode='draft') {
    const context=await browser.newContext({viewport:{width:850,height:800},serviceWorkers:'block'});
    t.after(async()=>{await context.close();});
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>{
        const url=new URL(route.request().url());
        return url.origin===origin&&!url.pathname.startsWith('/api/')?route.continue():route.abort();
    });
    t.after(()=>{assert.deepEqual(errors,[]);});
    await page.goto(`${origin}/tests/ui/policy-review.html?mode=${mode}`,{
        waitUntil:'domcontentloaded',timeout:60000,
    });
    const candidateTab=page.getByRole('tab',{name:'정책 등록 후보',exact:false});
    await candidateTab.waitFor({state:'visible',timeout:60000});
    if(mode.startsWith('draft')) {
        await candidateTab.click();
        if(mode!=='draft-candidate')await page.getByRole('button',{name:'직접 찾은 정책으로 초안 만들기',exact:false}).click();
    }
    return page;
}
const finish=(page,kind,response,fail=false)=>page.evaluate(({kind,response,fail})=>window.finishDraft(kind,response,fail),{kind,response,fail});
const waitCount=(page,kind,n)=>page.waitForFunction(({kind,n})=>window.draftCalls[kind].length===n,{kind,n});
const input=page=>page.getByLabel('정책 안내 페이지 주소',{exact:true});
const create=page=>page.getByRole('button',{name:'확인하고 Notion에 추가',exact:true});
const check=page=>page.getByRole('button',{name:'Notion 등록 상태 확인',exact:true});
const sourceChecks=page=>page.getByRole('group',{name:'등록 전 원문 대조 확인',exact:true}).getByRole('checkbox');
async function confirmSource(page) {
    for(const checkbox of await sourceChecks(page).all())await checkbox.check();
}
async function preview(page,url) {
    await input(page).fill(url);
    await page.getByRole('button',{name:'정책 초안 만들기',exact:true}).click();
    await finish(page,'preview',previewFor(url));
    await page.getByRole('group',{name:'정책 초안 검토',exact:true}).waitFor();
    // Existing create/recovery scenarios now explicitly complete the new human source-review prerequisite.
    await confirmSource(page);
}

const documentConsent=page=>page.getByRole('checkbox',{name:'개인정보·비밀키가 없는 공개문서임을 직접 확인했으며, 이 파일을 Google AI에 전송하는 데 동의합니다.',exact:true});
const previewButton=page=>page.getByRole('button',{name:'정책 초안 만들기',exact:true});

test('빈 정책표 첫 안내에서 다른 표에 명시 등록한 뒤 그 표를 포함해 최신화로 이어진다',async t=>{
    const page=await fixture(t);
    await page.getByRole('tab',{name:'정책 최신화',exact:false}).click();
    const help=page.getByRole('note',{name:'첫 정책 시작 안내'});
    await help.waitFor();
    await page.getByRole('button',{name:'다른 정책표',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'다른 정책표',exact:true}).getAttribute('aria-pressed'),'false');
    await mkdir(artifactRoot,{recursive:true});
    await help.scrollIntoViewIfNeeded();
    await page.screenshot({path:`${artifactRoot}/first-policy-entry.png`,animations:'disabled'});
    await help.getByRole('button',{name:'첫 정책 초안 만들기',exact:true}).click();
    assert.equal(await page.getByRole('tab',{name:'정책 등록 후보',exact:false}).getAttribute('aria-selected'),'true');
    assert.equal(await input(page).isVisible(),true);
    const url='https://example.org/policy/first';
    await page.getByRole('combobox',{name:'저장할 보관함',exact:true}).selectOption('other');
    await input(page).fill(url);await previewButton(page).click();
    await waitCount(page,'preview',1);
    assert.equal(await page.evaluate(()=>window.draftCalls.preview[0][1]),'other');
    await finish(page,'preview',{...previewFor(url),db_key:'other'});
    await page.getByRole('group',{name:'정책 초안 검토'}).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),0);
    await confirmSource(page);await create(page).click();await waitCount(page,'create',1);
    assert.equal(await page.evaluate(()=>window.draftCalls.create[0][1]),'other');
    await finish(page,'create',{success:true});
    const next=page.getByRole('status',{name:'정책 추가 후 다음 단계'});
    await next.waitFor();
    assert.match(await next.innerText(),/첫 비교를 시작/);
    await next.scrollIntoViewIfNeeded();
    await page.screenshot({path:`${artifactRoot}/first-policy-next-step.png`,animations:'disabled'});
    await next.getByRole('button',{name:'정책 최신화로 이동'}).click();
    assert.equal(await page.getByRole('tab',{name:'정책 최신화',exact:false}).getAttribute('aria-selected'),'true');
    assert.equal(await page.getByRole('button',{name:'다른 정책표',exact:true}).getAttribute('aria-pressed'),'true');
    assert.equal(await page.getByRole('button',{name:'시험 정책표',exact:true}).getAttribute('aria-pressed'),'true','기존 선택도 유지');
    assert.equal(await page.getByRole('button',{name:'정책 정보 최신인지 확인하기',exact:true}).isEnabled(),true);
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),1);
});

test('여러 소득 기준의 초안도 원문과 함께 표시하고 확인 전 등록은 막는다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/policy/income';
    const income='복지급여: 기준 중위소득 65% 이하\n증명서 발급: 기준 중위소득 72% 이하';
    await input(page).fill(url);await previewButton(page).click();
    const payload=previewFor(url);
    payload.fields.target_income=income;
    payload.source_review={required:true,source_text:income,reasons:['draft_income_review_required'],unverified_fields:{target_income:null}};
    await finish(page,'preview',payload);
    const notice=page.getByRole('region',{name:'원문 대조 필요',exact:true});
    await notice.getByText('특히 대조할 항목: 소득 조건',{exact:true}).waitFor();
    await notice.getByText(/소득 기준이 여러 개입니다/).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    await confirmSource(page);
    assert.equal(await create(page).isEnabled(),true);
    await create(page).click();await waitCount(page,'create',1);
    assert.equal(await page.evaluate(()=>window.draftCalls.create[0][2].target_income),income);
    await finish(page,'create',{success:true});
});

for(const [extension,analyzedFrom] of [['pdf','document'],['png',undefined]]) {
    test(`${extension} 초안 등록 후 자동 재분석 범위를 안내하며 전송 동의를 재사용하지 않는다`,async t=>{
        const page=await fixture(t);
        const url=`https://example.org/policy/source.${extension}`;
        await input(page).fill(url);await documentConsent(page).check();
        await previewButton(page).click();await waitCount(page,'preview',1);
        await finish(page,'preview',{...previewFor(url),analyzed_from:analyzedFrom});
        await page.getByRole('group',{name:'정책 초안 검토'}).waitFor();
        assert.equal(await documentConsent(page).isChecked(),false);
        await confirmSource(page);await create(page).click();await waitCount(page,'create',1);
        await finish(page,'create',{success:true});
        const next=page.getByRole('status',{name:'정책 추가 후 다음 단계'});
        await next.waitFor();
        assert.match(await next.innerText(),/PDF·이미지 원문을 자동으로 다시 분석하지 않습니다/);
        assert.match(await next.innerText(),/문서 출처 주소를.*HTML 안내 페이지로 바꾼/);
        assert.match(await next.innerText(),/파일 전송 동의는 이번 초안 분석에만/);
        assert.doesNotMatch(await next.innerText(),/첫 비교를 시작/);
        await next.getByRole('button',{name:'정책 최신화로 이동'}).click();
        assert.equal(await page.getByRole('tab',{name:'정책 최신화',exact:false}).getAttribute('aria-selected'),'true');
        assert.equal(await page.evaluate(()=>window.draftCalls.preview.length),1,'화면 이동은 파일을 재전송하지 않음');
        assert.equal(await page.evaluate(()=>window.draftCalls.create.length),1);
    });
}

test('원문 대조는 상시 안내이며 미확인 등록을 막고 전사문을 실행 없는 텍스트로 표시한다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/policy/source-review';
    const source='<img src=x onerror="window.sourceScriptExecuted=true">\n신청: 8월 4일~25일\n이용: 9월~12월';
    await input(page).fill(url);await previewButton(page).click();
    await finish(page,'preview',{...previewFor(url),source_review:{required:true,source_text:source,
        reasons:['model_text_unverified'],unverified_fields:{support_detail:'임의 본문',contact:'임의 연락처'},note:'임의 진단 원문'}});
    const notice=page.getByRole('region',{name:'원문 대조 필요',exact:true});
    await notice.waitFor();
    await notice.getByText('특히 대조할 항목: 지원 내용·비용 · 연락처',{exact:true}).waitFor();
    assert.equal(await notice.isVisible(),true);
    assert.equal(await sourceChecks(page).count(),1);
    assert.deepEqual(await sourceChecks(page).evaluateAll(items=>items.map(item=>item.checked)),[false]);
    await page.getByRole('checkbox',{name:'원문과 비교하여 초안을 확인했습니다. 확인되지 않은 내용은 수정하거나 비워두었습니다.',exact:true}).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    await create(page).evaluate(el=>{el.disabled=false;el.click();el.disabled=true;});
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),0,'disabled를 우회해도 handler가 확인을 다시 검사');
    await notice.getByText('AI가 읽은 문장 · 원본과 대조해주세요',{exact:true}).click();
    const transcript=page.getByRole('region',{name:'AI가 읽은 문장',exact:true});
    assert.equal(await transcript.innerText(),source);
    assert.equal(await transcript.locator('img,script').count(),0);
    assert.equal(await page.evaluate(()=>window.sourceScriptExecuted),undefined);
    assert.equal(await page.getByText('임의 진단 원문',{exact:true}).count(),0);
    for(const [width,dark] of [[390,false],[1280,true]]) {
        await page.setViewportSize({width,height:900});
        await page.addStyleTag({content:'aside{display:none}main{padding:12px!important}'});
        await page.evaluate(dark=>{document.documentElement.classList.toggle('dark',dark);document.documentElement.style.fontSize='20px';},dark);
        assert.equal(await notice.evaluate(el=>el.scrollWidth>el.clientWidth),false);
        assert.equal(await page.getByRole('group',{name:'정책 초안 검토',exact:true}).evaluate(el=>el.scrollWidth>el.clientWidth),false);
    }
    const checks=sourceChecks(page);
    await checks.first().focus();await page.keyboard.press('Space');
    assert.equal(await create(page).isEnabled(),true);
    await page.keyboard.press('Space');
    assert.equal(await create(page).isDisabled(),true);
    await page.keyboard.press('Space');
    assert.equal(await create(page).isEnabled(),true);
    await create(page).click();await waitCount(page,'create',1);
    assert.equal(await page.evaluate(()=>window.draftCalls.create[0][3]),true);
    assert.equal(await page.evaluate(()=>Object.hasOwn(window.draftCalls.create[0][2],'source_review')),false);
    await finish(page,'create',{success:true});
});

test('파일 전송 동의와 원문 확인은 독립이며 필드·보관함·URL·새 미리보기 변경은 원문 확인을 해제한다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/source.pdf';
    await input(page).fill(url);await documentConsent(page).check();await previewButton(page).click();
    await finish(page,'preview',{...previewFor(url),source_review:{required:true,source_text:'모의 원문',unverified_fields:[]}});
    await sourceChecks(page).first().waitFor();
    assert.equal(await documentConsent(page).isChecked(),false);
    assert.equal(await create(page).isDisabled(),true);
    await confirmSource(page);
    assert.equal(await documentConsent(page).isChecked(),false,'사람의 대조 확인이 외부 전송 동의로 바뀌지 않음');
    assert.equal(await previewButton(page).isDisabled(),true);
    await page.getByLabel('상세 지원 내용',{exact:true}).fill('직접 수정한 모의 지원 내용');
    assert.deepEqual(await sourceChecks(page).evaluateAll(items=>items.map(item=>item.checked)),[false]);
    assert.equal(await create(page).isDisabled(),true);
    await create(page).evaluate(el=>{el.disabled=false;el.click();el.disabled=true;});
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),0);
    await confirmSource(page);await page.getByLabel('시작 월령',{exact:true}).fill('1');
    assert.equal(await sourceChecks(page).first().isChecked(),false);
    await confirmSource(page);await page.getByRole('group',{name:'정책 초안 검토',exact:true}).getByRole('button',{name:'전국',exact:true}).click();
    assert.equal(await sourceChecks(page).first().isChecked(),false);
    await confirmSource(page);await documentConsent(page).check();await previewButton(page).click();
    assert.equal(await sourceChecks(page).count(),0);
    await finish(page,'preview',previewFor(url));
    await sourceChecks(page).first().waitFor();
    assert.equal(await sourceChecks(page).first().isChecked(),false,'구형 메타 없는 재응답도 자동 확인 아님');
    await page.getByText('이 미리보기에는 대조용 문장을 표시할 수 없습니다.',{exact:false}).waitFor();
    await confirmSource(page);await page.getByLabel('저장할 보관함',{exact:true}).selectOption('other');
    assert.equal(await sourceChecks(page).count(),0);
    await input(page).fill('https://example.org/another');await previewButton(page).click();
    await finish(page,'preview',previewFor('https://example.org/another'));
    await sourceChecks(page).first().waitFor();
    assert.equal(await sourceChecks(page).first().isChecked(),false);
    assert.equal(await documentConsent(page).count(),0);
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),0);
});

test('PDF·이미지는 기본 미동의이며 확인은 이번 요청에만 적용되고 실패 재시도에도 다시 필요하다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/source.pdf';
    await input(page).fill(url);
    const consent=documentConsent(page);
    assert.equal(await consent.isChecked(),false);
    assert.equal(await previewButton(page).isDisabled(),true);
    await input(page).press('Enter');
    assert.equal(await page.evaluate(()=>window.draftCalls.preview.length),0);
    await consent.focus();await page.keyboard.press('Space');
    assert.equal(await consent.isChecked(),true);
    await previewButton(page).click();await waitCount(page,'preview',1);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.preview[0]),[url,'test','',true]);
    assert.equal(await consent.isChecked(),false);
    assert.equal(await consent.isDisabled(),true);
    await finish(page,'preview','모의 분석 실패',true);
    await page.waitForFunction(()=>!document.querySelector('#policy-url-draft-panel input[type=checkbox]').disabled);
    assert.equal(await previewButton(page).isDisabled(),true);
    await consent.check();await previewButton(page).click();await waitCount(page,'preview',2);
    await finish(page,'preview',previewFor(url));
    await page.getByRole('group',{name:'정책 초안 검토',exact:true}).waitFor();
    assert.equal(await consent.isChecked(),false);
});

test('파일 URL 교체는 동의를 초기화하며 일반 HTML 초안에는 문서 동의를 강요하지 않는다',async t=>{
    const page=await fixture(t);
    await input(page).fill('https://example.org/a.pdf');
    await documentConsent(page).check();
    await input(page).fill('https://example.org/b.png');
    assert.equal(await documentConsent(page).isChecked(),false);
    assert.equal(await previewButton(page).isDisabled(),true);
    const html='https://example.org/policy';
    await input(page).fill(html);
    assert.equal(await documentConsent(page).count(),0);
    assert.equal(await previewButton(page).isDisabled(),false);
    await previewButton(page).click();await waitCount(page,'preview',1);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.preview[0]),[html,'test','',false]);
    await finish(page,'preview',previewFor(html));
    await page.getByRole('group',{name:'정책 초안 검토',exact:true}).waitFor();
});

test('확장자 없는 다운로드의 문서 발견 오류는 자동 재전송 없이 파일 확인을 요구한다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/download?id=1';
    await input(page).fill(url);
    await previewButton(page).click();await waitCount(page,'preview',1);
    await finish(page,'preview',{response:{status:409,data:{detail:{error:'AI_DOCUMENT_REVIEW_REQUIRED',message:'원문 파일을 확인해주세요.'}}}},true);
    await documentConsent(page).waitFor();
    assert.equal(await documentConsent(page).isChecked(),false);
    assert.equal(await previewButton(page).isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.draftCalls.preview.length),1);
    await documentConsent(page).check();await previewButton(page).click();await waitCount(page,'preview',2);
    assert.equal(await page.evaluate(()=>window.draftCalls.preview[1][3]),true);
    await input(page).fill('https://example.org/new.pdf');
    await finish(page,'preview',previewFor(url));
    assert.equal(await documentConsent(page).isChecked(),false);
    assert.equal(await page.getByRole('group',{name:'정책 초안 검토',exact:true}).count(),0);
});

test('URL·보관함 변경은 이전 미리보기와 늦은 등록 상태·재시도 정보를 폐기한다',async t=>{
    const page=await fixture(t);
    const a='https://example.org/policy/a',b='https://example.org/policy/b',c='https://example.org/policy/c';
    await input(page).fill(a);
    await page.getByRole('button',{name:'정책 초안 만들기',exact:true}).evaluate(el=>{el.click();el.click();});
    await waitCount(page,'preview',1);
    await input(page).fill(b);
    await finish(page,'preview',previewFor(a));
    await page.getByRole('button',{name:'정책 초안 만들기',exact:true}).waitFor();
    assert.equal(await page.getByRole('group',{name:'정책 초안 검토',exact:true}).count(),0);
    await check(page).evaluate(el=>{el.click();el.click();});
    await waitCount(page,'status',1);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.status),[[b]]);
    await input(page).fill(c);
    await finish(page,'status',retryStatus(b));
    await check(page).waitFor();
    assert.equal(await page.getByRole('button',{name:'Notion 미등록 확인 후 재시도 허용',exact:true}).count(),0);
    assert.equal(await page.locator('[data-url-draft-recovery] [role=status]').count(),0);
    await check(page).click();await finish(page,'status',retryStatus(c));
    await page.getByText(`확인할 출처: ${c}`,{exact:true}).waitFor();
    await page.getByLabel('저장할 보관함',{exact:true}).selectOption('other');
    assert.equal(await page.locator('[data-url-draft-recovery] [role=status]').count(),0);
    await preview(page,a);
    await input(page).fill(b);
    assert.equal(await page.getByRole('group',{name:'정책 초안 검토',exact:true}).count(),0);
});

test('불확실 등록 후 상태 확인·명시적 미등록 허용은 자동 생성하지 않고 중복 요청을 막는다',async t=>{
    const page=await fixture(t);
    const url='https://example.org/policy/retry';
    await preview(page,url);
    await create(page).evaluate(el=>{el.click();el.click();});
    await waitCount(page,'create',1);
    assert.equal(await input(page).isDisabled(),true);
    assert.equal(await check(page).isDisabled(),true);
    await finish(page,'create','응답이 끊겼습니다',true);
    await page.getByText('등록 여부를 확인하지 못했습니다. 다시 추가하기 전에 Notion 등록 상태를 확인해주세요.',{exact:true}).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    await check(page).evaluate(el=>{el.click();el.click();});await waitCount(page,'status',1);
    await finish(page,'status',retryStatus(url));
    await page.getByRole('link',{name:'원래 정책표 열기',exact:true}).waitFor();
    await page.evaluate(()=>{window.retryConfirmations=[];window.confirm=message=>{window.retryConfirmations.push(message);return true;};});
    const retry=page.getByRole('button',{name:'Notion 미등록 확인 후 재시도 허용',exact:true});
    await retry.evaluate(el=>{el.click();el.click();});await waitCount(page,'retry',1);
    assert.equal(await input(page).isDisabled(),true);
    assert.equal(await create(page).isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.retry),[[url,'fixture-attempt']]);
    assert.match((await page.evaluate(()=>window.retryConfirmations))[0],/https:\/\/example.org\/policy\/retry/);
    assert.equal((await page.evaluate(()=>window.retryConfirmations)).length,1);
    await finish(page,'retry',{message:'다시 추가할 수 있습니다.'});
    await page.waitForFunction(()=>!document.querySelector('fieldset[aria-label="정책 초안 검토"]').disabled);
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),1,'재시도 허용은 생성 호출이 아님');
    assert.equal(await create(page).isDisabled(),false);
    await create(page).click();await waitCount(page,'create',2);
    await finish(page,'create',{success:true});
    await page.getByRole('group',{name:'정책 초안 검토',exact:true}).waitFor({state:'hidden'});
    assert.equal(await input(page).inputValue(),'');
});

test('상태 확인 실패는 재생성을 열지 않고 같은 자리에서 다시 확인할 수 있다',async t=>{
    const page=await fixture(t);
    await preview(page,'https://example.org/policy/status');
    await check(page).click();await finish(page,'status','상태 조회 끊김',true);
    await page.getByText('등록 상태를 확인하지 못했습니다. 잠시 후 다시 확인해주세요.',{exact:true}).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    const recovery=page.locator('[data-url-draft-recovery]');
    assert.equal(await recovery.evaluate(el=>getComputedStyle(el).borderTopWidth),'0px','복구 안내는 새 카드가 아닌 텍스트');
    await check(page).click();await finish(page,'status',{state:'completed',can_retry:false,message:'이미 등록된 정책입니다.',deeplink:'https://www.notion.so/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'});
    await page.getByRole('link',{name:'등록된 Notion 페이지 열기',exact:true}).waitFor();
    assert.equal(await create(page).isDisabled(),true);
    assert.equal(await page.evaluate(()=>window.draftCalls.create.length),0);
    await recovery.scrollIntoViewIfNeeded();
    await mkdir(`${artifactRoot}/policy-url-draft`,{recursive:true});
    await page.screenshot({path:`${artifactRoot}/policy-url-draft/recovery-850.png`,animations:'disabled'});
    assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
});

test('정책 후보의 불확실 등록도 해당 출처와 보관함으로 같은 복구 경로를 연다',async t=>{
    const page=await fixture(t,'draft-candidate');
    const add=page.getByRole('button',{name:'Notion에 초안 추가',exact:true});
    await add.evaluate(el=>{el.click();el.click();});await waitCount(page,'candidate',1);
    assert.equal(await add.isDisabled(),true);
    await finish(page,'candidate','후보 생성 응답 끊김',true);
    await page.getByText('이 후보의 등록 여부를 확인하지 못했습니다.',{exact:false}).waitFor();
    assert.equal(await input(page).inputValue(),'https://example.org/policy/candidate');
    assert.equal(await page.getByLabel('저장할 보관함',{exact:true}).inputValue(),'test');
    assert.equal(await add.isDisabled(),true);
    await check(page).click();await waitCount(page,'status',1);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.status),[['https://example.org/policy/candidate']]);
    await finish(page,'status',{state:'completed',message:'이미 등록됐습니다.',can_retry:false});
    assert.equal(await page.evaluate(()=>window.draftCalls.candidate.length),1);
});

test('등록 후보 탭은 URL 초안과 범위를 공유하며 이동·키보드 조작만으로 저장하지 않는다',async t=>{
    const page=await fixture(t,'draft-candidate');
    const tab=page.getByRole('tab',{name:'정책 등록 후보',exact:false});
    const panel=page.getByRole('tabpanel',{name:'정책 등록 후보',exact:false});
    assert.equal(await tab.getAttribute('aria-selected'),'true');
    assert.equal(await page.getByText('연결한 정책표의 출처 페이지',{exact:false}).count(),0,'후보 탭에는 기존 정책 점검 설명을 중복하지 않음');
    await panel.getByRole('heading',{name:'수집 자료에서 찾은 후보',exact:true}).waitFor();
    assert.equal(await page.getByRole('heading',{name:'정책 내용 변경 감지',exact:false}).count(),0);
    await panel.getByRole('button',{name:'직접 찾은 정책으로 초안 만들기',exact:false}).click();
    await input(page).fill('https://example.org/policy/retained');
    await page.getByLabel('확인이 필요한 정책 후보 저장할 정책표',{exact:true}).selectOption('other');
    await tab.focus();await page.keyboard.press('Home');
    await page.getByRole('tab',{name:'정책 최신화',exact:false}).waitFor();
    assert.equal(await page.getByRole('tab',{name:'정책 최신화',exact:false}).getAttribute('aria-selected'),'true');
    assert.equal(await page.getByRole('button',{name:'Notion에 초안 추가',exact:true}).count(),0);
    await page.keyboard.press('End');
    assert.equal(await tab.getAttribute('aria-selected'),'true');
    assert.equal(await input(page).inputValue(),'https://example.org/policy/retained');
    assert.equal(await page.getByLabel('확인이 필요한 정책 후보 저장할 정책표',{exact:true}).inputValue(),'other');
    await page.getByRole('group',{name:'비교할 정책표',exact:true}).getByRole('button',{name:'다른 정책표',exact:true}).click();
    await page.waitForFunction(()=>window.candidateScopes.at(-1)?.join(',')==='test');
    assert.equal(await page.evaluate(()=>Object.values(window.draftCalls).flat().length),0,'탭과 비교 범위 변경은 AI/Notion 생성 호출 없음');
    assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth>el.clientWidth),false);
    await mkdir(`${artifactRoot}/policy-field-groups`,{recursive:true});
    await panel.screenshot({path:`${artifactRoot}/policy-field-groups/candidate-tab-850.png`,animations:'disabled'});
});

test('후보 제외의 늦은 성공은 다른 탭에서 새로 선택한 정책 제안을 지우지 않는다',async t=>{
    const page=await fixture(t,'candidate-dismiss');
    await page.getByRole('tab',{name:'정책 등록 후보',exact:false}).click();
    await page.evaluate(()=>{window.confirm=()=>true;});
    await page.getByRole('button',{name:'무시',exact:true}).evaluate(el=>{el.click();el.click();});
    await waitCount(page,'dismiss',1);
    assert.equal(await page.getByRole('button',{name:'제외 중…',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'Notion에 초안 추가',exact:true}).isDisabled(),true);
    assert.equal(await page.getByLabel('확인이 필요한 정책 후보 저장할 정책표',{exact:true}).isDisabled(),true);
    const mainTab=page.getByRole('tab',{name:'정책 최신화',exact:false});
    assert.equal(await mainTab.isEnabled(),true,'별개인 정책 검토는 계속할 수 있음');
    await mainTab.click();
    await page.locator('button[aria-controls="policy-detail-1"]').click();
    await page.locator('#policy-review-1').getByRole('button',{name:'반영할 내용 선택',exact:true}).click();
    const selected=page.getByRole('region',{name:'선택한 정책 작업'}).getByRole('button',{name:'선택한 내용 Notion에 반영 (1)',exact:true});
    await selected.waitFor();
    const scopeCalls=await page.evaluate(()=>window.candidateScopes.length);
    await finish(page,'dismiss',{success:true});
    await page.waitForFunction(previous=>window.candidateScopes.length>previous,scopeCalls);
    assert.equal(await selected.isEnabled(),true,'늦게 완료된 후보 제외는 정책 선택을 초기화하지 않음');
    assert.match(await mainTab.innerText(),/선택 1개/);
    assert.deepEqual(await page.evaluate(()=>window.draftCalls.dismiss),[[[9]]]);
    assert.equal(await page.evaluate(()=>window.policyCalls.length+window.individualCalls.length+window.draftCalls.candidate.length),0);
    await page.getByRole('tab',{name:'정책 등록 후보',exact:false}).click();
    await page.getByText('정책표에 없는 신규 후보가 없습니다.',{exact:true}).waitFor();
    await mainTab.click();
    assert.equal(await selected.isEnabled(),true);
});

test('후보 제외 취소는 요청하지 않으며 실패 후에는 다시 제외할 수 있다',async t=>{
    const page=await fixture(t,'candidate-dismiss');
    await page.getByRole('tab',{name:'정책 등록 후보',exact:false}).click();
    const dismiss=page.getByRole('button',{name:'무시',exact:true});
    await page.evaluate(()=>{window.confirm=()=>false;});
    await dismiss.click();
    assert.equal(await page.evaluate(()=>window.draftCalls.dismiss.length),0);
    await page.evaluate(()=>{window.confirm=()=>true;});
    await dismiss.click();await waitCount(page,'dismiss',1);
    await finish(page,'dismiss','시험 제외 실패',true);
    await dismiss.waitFor();
    assert.equal(await dismiss.isEnabled(),true);
    assert.equal(await page.getByRole('button',{name:'Notion에 초안 추가',exact:true}).isEnabled(),true);
    await dismiss.click();await waitCount(page,'dismiss',2);
    await finish(page,'dismiss',{success:true});
    await page.getByText('정책표에 없는 신규 후보가 없습니다.',{exact:true}).waitFor();
});

test('일괄 반영이 불확실하면 뒤의 개별 확인 항목에도 요청하지 않고 선택을 보존한다',async t=>{
    const page=await fixture(t,'uncertain-bulk');
    await page.locator('button[aria-controls="policy-detail-1"]').click();
    const card=page.locator('#policy-review-1');
    const select=card.getByRole('button',{name:'반영할 내용 선택',exact:true});
    await select.nth(1).click();await select.first().click();
    await page.evaluate(()=>{window.confirm=()=>true;});
    const bar=page.getByRole('region',{name:'선택한 정책 작업'});
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).click();
    await bar.getByText('추가 반영 중단 — Notion 확인 필요',{exact:false}).waitFor();
    assert.equal(await page.evaluate(()=>window.policyCalls.length),1);
    assert.equal(await page.evaluate(()=>window.individualCalls.length),0);
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).waitFor();
    await page.getByText('반영하지 못한 내용 확인 (2)',{exact:true}).click();
    await page.getByText('앞선 항목의 반영 여부를 확인하지 못해 요청하지 않았습니다.',{exact:false}).waitFor();
});

test('일부 적용 뒤 불확실하면 성공 선택은 빼고 불확실·미전송 항목만 유지한다',async t=>{
    const page=await fixture(t,'uncertain-bulk-partial');
    await page.locator('button[aria-controls="policy-detail-1"]').click();
    const card=page.locator('#policy-review-1');
    await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).evaluateAll(buttons=>buttons.forEach(button=>button.click()));
    await page.evaluate(()=>{window.confirm=()=>true;});
    const bar=page.getByRole('region',{name:'선택한 정책 작업'});
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (3)',exact:true}).click();
    await bar.getByText('추가 반영 중단 — Notion 확인 필요',{exact:false}).waitFor();
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.policyCalls[0].length),2);
    assert.equal(await page.evaluate(()=>window.individualCalls.length),0);
    assert.equal(await card.getByRole('button',{name:'반영할 내용 선택됨',exact:true}).count(),2);
    assert.equal(await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).count(),1,'성공한 일반 항목은 선택 해제');
    await bar.getByText('1건 적용',{exact:false}).waitFor();
});

test('개별 검토 중 불확실한 반영도 즉시 멈추고 앞선 성공을 제외한 선택만 유지한다',async t=>{
    const page=await fixture(t,'partial-review-uncertain');
    await page.locator('button[aria-controls="policy-detail-1"]').click();
    const card=page.locator('#policy-review-1');
    await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).evaluateAll(buttons=>buttons.forEach(button=>button.click()));
    await page.evaluate(()=>{window.confirm=()=>true;});
    const bar=page.getByRole('region',{name:'선택한 정책 작업'});
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (3)',exact:true}).click();
    await bar.getByText('추가 반영 중단 — Notion 확인 필요',{exact:false}).waitFor();
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.policyCalls.length),0);
    assert.equal(await page.evaluate(()=>window.individualCalls.length),2,'성공1건·불확실1건 이후 추가 요청 없음');
    assert.equal(await card.getByRole('button',{name:'반영할 내용 선택됨',exact:true}).count(),2);
    assert.equal(await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).count(),1);
    await bar.getByText('1건 적용',{exact:false}).waitFor();
});

test('명백한 개별 입력 오류를 불확실 반영과 혼동해 이후 확인한 항목을 막지 않는다',async t=>{
    const page=await fixture(t,'clear-review-error');
    await page.locator('button[aria-controls="policy-detail-1"]').click();
    const card=page.locator('#policy-review-1');
    await card.getByRole('button',{name:'반영할 내용 선택',exact:true}).evaluateAll(buttons=>buttons.forEach(button=>button.click()));
    await page.evaluate(()=>{window.confirm=()=>true;});
    const bar=page.getByRole('region',{name:'선택한 정책 작업'});
    await bar.getByRole('button',{name:'선택한 내용 Notion에 반영 (2)',exact:true}).click();
    await bar.getByText('1건 적용',{exact:false}).waitFor();
    assert.equal(await page.evaluate(()=>window.individualCalls.length),2);
    assert.equal(await bar.getByText('추가 반영 중단 — Notion 확인 필요',{exact:false}).count(),0);
});

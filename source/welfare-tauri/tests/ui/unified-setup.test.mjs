// Real React UI, synthetic in-memory config only. All non-fixture traffic is blocked.
import {before, after, test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1437';
let browser;
before(async () => {browser = await chromium.launch({headless:true, channel:'msedge'});});
after(async () => {await browser?.close();});
async function open(t, mode='정책', width=1100) {
    const context = await browser.newContext({viewport:{width,height:900}});
    const page = await context.newPage();
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.route('**/*', route=>{
        const url=new URL(route.request().url());
        return url.origin===origin && !url.pathname.startsWith('/api/') ? route.continue() : route.abort();
    });
    page.setDefaultTimeout(7000);
    t.after(async()=>{await context.close();assert.deepEqual(errors,[]);});
    await page.goto(`${origin}/tests/ui/settings-drafts.html`,{timeout:45000});
    await page.getByRole('link',{name:`${mode} 단계 시험`,exact:true}).click();
    await page.locator('#setup-section-google').waitFor();
    return page;
}
const settle=page=>page.evaluate(()=>globalThis.__fixtureFinishSave());
const control=(page,name)=>page.locator('aside').getByText(name,{exact:true}).evaluate(el=>el.click());
const stage=(page,n)=>page.getByRole('navigation',{name:/진행 현황/}).getByRole('button',{name:new RegExp(`^${n}\\.`)});

test('정책: Gemini → 가입 → API → 복제·권한 → 주소 등록을 한 흐름으로 완료',async t=>{
    const page=await open(t);
    assert.equal(await page.getByRole('navigation',{name:/진행 현황/}).getByRole('button').count(),5);
    assert.equal(await page.getByRole('button',{name:'다음 설명 보기',exact:true}).count(),0);
    await page.locator('#policy-google-key').fill('synthetic-key');
    await page.getByRole('button',{name:'저장하고 연결 확인',exact:true}).click();await settle(page);
    await page.locator('#setup-section-signup').waitFor();
    await page.getByRole('button',{name:/작업 공간 확인했어요/}).click();
    await page.locator('#onboarding-notion-token').fill('synthetic-token');
    await page.getByRole('button',{name:'연결 토큰 저장',exact:true}).click();await settle(page);
    await page.locator('#setup-section-template').waitFor();
    assert.equal(await page.getByRole('button',{name:'설정 매뉴얼 열기',exact:true}).count(),1);
    assert.equal(await page.getByRole('link',{name:/정책표 템플릿 열기/}).count(),1);
    await page.getByRole('button',{name:/복제·권한 설정했어요/}).click();
    await page.locator('#setup-section-policy input').first().fill('https://www.notion.so/synthetic-policy');
    await page.getByRole('button',{name:'저장하고 정책표 연결 확인',exact:true}).click();await settle(page);
    await page.getByRole('group',{name:'시작 안내 선택',exact:true}).waitFor();
    await page.getByRole('link').filter({hasText:'정책 최신화'}).filter({hasText:'설정 완료'}).waitFor();
    const payloads=await page.evaluate(()=>globalThis.__fixtureConfigSaves);
    assert.equal(payloads.length,3,'키·토큰·표 주소만 저장, 안내 이동 자체는 저장하지 않음');
});

test('새 소식: AI 없이 가입·토큰·복제·주소 등록, 저장 실패 뒤 입력 보존',async t=>{
    const page=await open(t,'보관함');
    await page.getByRole('button',{name:'AI는 나중에 연결',exact:true}).click();
    await page.getByRole('button',{name:/작업 공간 확인했어요/}).click();
    await page.locator('#onboarding-notion-token').fill('synthetic-token');
    await page.getByRole('button',{name:'연결 토큰 저장',exact:true}).click();await settle(page);
    await page.getByRole('link',{name:/새 소식 보관함 템플릿 열기/}).waitFor();
    await page.getByRole('button',{name:/복제·권한 설정했어요/}).click();
    const input=page.locator('#onboarding-news-db');
    await input.fill('https://www.notion.so/synthetic-news');
    await control(page,'다음 저장 실패');
    await page.getByRole('button',{name:'저장하고 보관함 연결 확인',exact:true}).click();await settle(page);
    await page.getByRole('status').filter({hasText:'새 소식 보관함를 연결하지 못했습니다.'}).waitFor();
    assert.equal(await input.inputValue(),'https://www.notion.so/synthetic-news');
    await page.getByRole('button',{name:'저장하고 보관함 연결 확인',exact:true}).click();await settle(page);
    await page.getByRole('group',{name:'시작 안내 선택',exact:true}).waitFor();
    assert.equal((await page.evaluate(()=>globalThis.__fixtureReadConfig())).is_configured,false);
});

test('완료된 보관함은 등록값을 보여주고 같은 화면에서 재편집한다',async t=>{
    const page=await open(t,'보관함');
    await control(page,'모두 연결');
    await page.evaluate(()=>globalThis.__fixtureRefreshQueries());
    await stage(page,5).click();
    await page.getByText('등록된 보관함 주소 또는 ID',{exact:true}).waitFor();
    await page.getByRole('button',{name:'새 소식 보관함 연결·변경',exact:true}).click();
    await page.locator('#onboarding-news-db').waitFor();
    assert.notEqual(await page.locator('#onboarding-news-db').inputValue(),'');
    assert.equal((await page.evaluate(()=>globalThis.__fixtureConfigSaves)).length,0);
});

test('연결 관리의 보관함 변경 딥링크도 완료 상태에서 입력칸을 연다',async t=>{
    const page=await open(t,'보관함');
    await control(page,'모두 연결');
    await control(page,'예전 Notion 설정 주소');
    await page.getByRole('link',{name:'Notion 새 소식 연결',exact:true}).click();
    await page.locator('#onboarding-news-db').waitFor();
});

for(const width of [390,1100]) test(`${width}px: 단계 표시·주소 등록에 가로 넘침 없이 키보드로 이동`,async t=>{
    const page=await open(t,'보관함',width);
    await stage(page,4).click();
    await page.locator('#setup-section-template').waitFor();
    assert.equal(await page.locator('dialog').evaluate(el=>el.scrollWidth>el.clientWidth),false);
    await page.screenshot({path:`../artifacts/tutorial-flow-20260914/template-${width}.png`});
    await stage(page,5).focus();await page.keyboard.press('Enter');
    await page.locator('#onboarding-news-db').waitFor();
    assert.equal(await page.locator('dialog').evaluate(el=>el.scrollWidth>el.clientWidth),false);
    await page.screenshot({path:`../artifacts/tutorial-flow-20260914/register-${width}.png`});
});

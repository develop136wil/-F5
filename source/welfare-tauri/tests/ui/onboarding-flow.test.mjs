// Run against the isolated Vite fixture; no real API keys or backend are used.
// PLAYWRIGHT_MODULE may point to an already installed Playwright package.
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';
let browser;
before(async () => { browser = await chromium.launch({ headless: true, channel: process.env.UI_TEST_BROWSER || 'msedge' }); });
after(async () => { await browser?.close(); });

async function fixture(t, mode = 'AI') {
    const context = await browser.newContext({ viewport: { width: 1100, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        return url.origin === origin && !url.pathname.startsWith('/api/') ? route.continue() : route.abort();
    });
    page.setDefaultTimeout(5000);
    t.after(async () => { await context.close(); assert.deepEqual(errors, [], '브라우저 실행 오류'); });
    // Cold Vite compilation can exceed the interaction timeout on this PC.
    // Keep individual action assertions at 5 seconds; allow initial loading longer.
    await page.goto(`${origin}/tests/ui/settings-drafts.html`, {timeout:60000,waitUntil:'domcontentloaded'});
    await page.getByRole('link', { name: `${mode} 단계 시험`, exact: true }).waitFor({timeout:60000});
    await page.getByRole('link', { name: `${mode} 단계 시험`, exact: true }).click();
    if (mode === '보관함') await page.getByRole('button', {name:/^3\. Notion API/}).click();
    return page;
}
const keyInput = page => page.locator('#onboarding-google-key, #policy-google-key');
const saveKey = page => page.getByRole('button', { name: '저장하고 연결 확인', exact: true });
const settleSave = async page => {
    await page.waitForFunction(() => globalThis.__fixtureHasPendingSave());
    await page.evaluate(() => globalThis.__fixtureFinishSave());
};
// Test-only controls inject external replies/navigation even while the modal makes the real background inert.
const fixtureControl = (page, name) => page.locator('aside').getByText(name, {exact:true}).evaluate(async el => {
    el.click();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
});
const draftKey = page => page.evaluate(() => globalThis.__fixtureSettingsDrafts.get('apiKey'));

test('완료 화면에서 연결 상태가 사라지면 완료 안내를 유지하지 않는다', async t => {
    const page = await fixture(t);
    await fixtureControl(page, '모두 연결');
    await fixtureControl(page, '정책 단계 시험');
    // Completion is the persistent work link, not a transient no-section banner.
    const startPolicy=page.getByRole('link',{name:'정책 최신화 화면으로 이동',exact:true});
    await startPolicy.waitFor();
    await fixtureControl(page, '모두 미설정');
    await page.evaluate(() => globalThis.__fixtureRefreshQueries());
    await keyInput(page).waitFor();
    assert.equal(await startPolicy.count(), 0);
    assert.equal(await page.getByText('필요한 연결을 준비했습니다.', {exact:false}).count(), 0);
});

test('한 번의 키 저장은 지연 연결 확인도 한 번만 요청한다', async t => {
    const page = await fixture(t, '정책');
    await page.evaluate(() => { globalThis.__fixtureVerificationDelay = 100; globalThis.__fixtureGoogleVerifications = 0; });
    await keyInput(page).fill('fixture-delayed-key');
    await saveKey(page).click();
    await settleSave(page);
    await page.getByRole('button', {name:/작업 공간 확인했어요/}).click();
    await page.locator('#onboarding-notion-token').waitFor();
    assert.equal(await page.evaluate(() => globalThis.__fixtureGoogleVerifications), 1);
});

test('보관함 연결 해제는 취소 가능하고 토큰·정책 연결을 보존한다', async t => {
    const page = await fixture(t);
    await fixtureControl(page, '예전 Notion 설정 주소');
    await fixtureControl(page, '모두 연결');
    // The legacy address opens connection management; edit the gallery from there.
    await page.getByRole('link',{name:/Notion\s+새 소식 연결/}).click();
    const before = await page.evaluate(() => globalThis.__fixtureReadConfig());
    const savedBefore = await page.evaluate(() => globalThis.__fixtureConfigSaves.length);
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('button', {name:'새 소식 보관함 연결 해제', exact:true}).click();
    assert.equal((await page.evaluate(() => globalThis.__fixtureReadConfig())).NOTION_DATABASE_ID, before.NOTION_DATABASE_ID);
    assert.equal(await page.evaluate(() => globalThis.__fixtureConfigSaves.length), savedBefore);
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', {name:'새 소식 보관함 연결 해제', exact:true}).click();
    await settleSave(page);
    await page.getByRole('status').filter({hasText:'새 소식 보관함 연결을 해제했습니다.'}).waitFor();
    const after = await page.evaluate(() => globalThis.__fixtureReadConfig());
    assert.equal(after.NOTION_DATABASE_ID, '');
    assert.equal(after.NOTION_POLICY_DBS, before.NOTION_POLICY_DBS);
    assert.equal(after.notion_token_configured, true);
});
async function expectVisible(locator) { await locator.waitFor({ state: 'visible' }); }
async function expectHidden(locator) { await locator.waitFor({ state: 'hidden' }); }

test('지연 저장 중 중복 전송을 막고, 실패하면 입력과 현재 단계를 유지한다', async t => {
    const page = await fixture(t);
    await keyInput(page).fill('fixture-key-original');
    await fixtureControl(page, '다음 저장 실패');
    await saveKey(page).click();
    assert.equal(await saveKey(page).isDisabled(), true);
    assert.equal(await draftKey(page), 'fixture-key-original');
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: 'AI 연결 중 오류가 발생했습니다.' }));
    assert.equal(await keyInput(page).inputValue(), 'fixture-key-original');
    assert.equal(await saveKey(page).isEnabled(), true);
    await expectVisible(page.getByText(/실제 API\/운영 설정 접근 없음 · 요청 1회/));
    await saveKey(page).click();
    await settleSave(page);
    await expectVisible(page.getByRole('heading', { name: '수집 대상', exact: true }));
    assert.equal(await draftKey(page), '');
});

test('저장은 성공해도 연결 확인 실패 시 다음 단계로 넘기지 않는다', async t => {
    const page = await fixture(t, '정책');
    await fixtureControl(page, '연결 확인 실패 설정');
    await keyInput(page).fill('fixture-rejected-key');
    await saveKey(page).click();
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: '연결하지 못했습니다. 입력한 Gemini API 키와 인터넷 연결을 확인해주세요.' }));
    await expectVisible(page.locator('#setup-section-google'));
    await expectHidden(page.locator('#setup-section-notion'));
});

test('정책 AI 저장 성공 후 가입 확인을 거쳐 토큰 단계만 펼쳐진다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-policy-key');
    await saveKey(page).click();
    await settleSave(page);
    await page.getByRole('button', {name:/작업 공간 확인했어요/}).click();
    await expectVisible(page.locator('#onboarding-notion-token'));
    await expectHidden(page.locator('#setup-section-google'));
    await expectHidden(page.locator('#setup-section-policy'));
});

test('보관함 토큰 저장 실패 후 재시도하면 보관함 단계로 이동한다', async t => {
    const page = await fixture(t, '보관함');
    const input = page.locator('#onboarding-notion-token');
    await input.fill('fixture-notion-token');
    await fixtureControl(page, '다음 저장 실패');
    await page.getByRole('button', { name: '연결 토큰 저장', exact: true }).click();
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: 'Notion 연결 토큰을 저장하지 못했습니다.' }));
    assert.equal(await input.inputValue(), 'fixture-notion-token');
    await page.getByRole('button', { name: '연결 토큰 저장', exact: true }).click();
    await settleSave(page);
    await page.getByRole('button', {name:/복제·권한 설정했어요/}).click();
    await expectVisible(page.locator('#onboarding-news-db'));
    await expectHidden(page.locator('#setup-section-notion'));
});

test('저장 중 다른 기능으로 이동하면 늦은 실패 안내가 새 기능에 나타나지 않는다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-late-key');
    await fixtureControl(page, '다음 저장 실패');
    await saveKey(page).click();
    await fixtureControl(page, '보관함 단계 시험');
    await settleSave(page);
    await expectVisible(page.locator('#setup-section-google'));
    assert.equal(await draftKey(page), 'fixture-late-key');
    assert.equal(await page.getByText('AI 연결 중 오류가 발생했습니다.', { exact: true }).count(), 0);
});

test('저장 요청 뒤 수정한 새 입력을 완료 처리하거나 숨기지 않는다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-old-key');
    await saveKey(page).click();
    await keyInput(page).fill('fixture-new-key');
    await settleSave(page);
    await expectVisible(keyInput(page));
    assert.equal(await keyInput(page).inputValue(), 'fixture-new-key');
    assert.equal(await draftKey(page), 'fixture-new-key');
    await expectHidden(page.locator('#setup-section-notion'));
});

test('저장 중 같은 안내를 나갔다 돌아와도 이전 요청이 단계를 넘기지 않는다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-roundtrip-key');
    await saveKey(page).click();
    await fixtureControl(page, '보관함 단계 시험');
    await fixtureControl(page, '정책 단계 시험');
    await settleSave(page);
    await expectVisible(page.locator('#setup-section-google'));
    await expectHidden(page.locator('#setup-section-notion'));
});

async function connectToken(page) {
    const signup = page.getByRole('button', {name:/작업 공간 확인했어요/});
    if (await signup.isVisible()) await signup.click();
    await page.locator('#onboarding-notion-token').fill('fixture-shared-token');
    await page.getByRole('button', { name: '연결 토큰 저장', exact: true }).click();
    await settleSave(page);
    await expectHidden(page.locator('#setup-section-notion'));
    await page.getByRole('button', {name:/복제·권한 설정했어요/}).click();
}

test('토큰 저장 중 수정한 새 토큰도 숨기지 않는다', async t => {
    const page = await fixture(t, '보관함');
    const input = page.locator('#onboarding-notion-token');
    await input.fill('fixture-old-token');
    await page.getByRole('button', { name: '연결 토큰 저장', exact: true }).click();
    await input.fill('fixture-new-token');
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: '그 뒤에 수정한 내용은 아직 저장되지 않았습니다.' }));
    assert.equal(await input.inputValue(), 'fixture-new-token');
    await expectHidden(page.locator('#setup-section-news'));
});

test('보관함 주소 저장 중 수정한 새 주소는 남기고 재저장할 수 있다', async t => {
    const page = await fixture(t, '보관함');
    await connectToken(page);
    const input = page.locator('#onboarding-news-db');
    await input.fill('https://www.notion.so/fixture-old-gallery');
    await page.getByRole('button', { name: '저장하고 보관함 연결 확인', exact: true }).click();
    await input.fill('https://www.notion.so/fixture-new-gallery');
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: '그 뒤에 수정한 내용은 아직 저장되지 않았습니다.' }));
    assert.equal(await input.inputValue(), 'https://www.notion.so/fixture-new-gallery');
    await page.getByRole('button', { name: '저장하고 보관함 연결 확인', exact: true }).click();
    await settleSave(page);
    // A finished feature returns to the guide index. It must save the newer URL
    // without silently treating the skipped-over Gemini step as configured.
    await expectHidden(page.getByRole('dialog'));
    await expectVisible(page.getByRole('group',{name:'시작 안내 선택',exact:true}));
    const saved = await page.evaluate(() => globalThis.__fixtureReadConfig());
    assert.equal(saved.NOTION_DATABASE_ID, 'https://www.notion.so/fixture-new-gallery');
    assert.equal(saved.notion_configured, true);
    assert.equal(saved.is_configured, false);
    assert.deepEqual(await page.evaluate(() => globalThis.__fixtureConfigSaves
        .filter(payload=>Object.hasOwn(payload,'NOTION_DATABASE_ID')).map(payload=>payload.NOTION_DATABASE_ID)),
        ['https://www.notion.so/fixture-old-gallery','https://www.notion.so/fixture-new-gallery']);
    await fixtureControl(page, '보관함 단계 시험');
    await expectVisible(keyInput(page));
});

test('정책표 주소 저장 실패 시 유지하고 재시도 성공 후 완료한다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-policy-key');
    await saveKey(page).click();
    await settleSave(page);
    await connectToken(page);
    const input = page.locator('#setup-section-policy input').first();
    await input.fill('https://www.notion.so/fixture-policy');
    await fixtureControl(page, '다음 저장 실패');
    await page.getByRole('button', { name: '저장하고 정책표 연결 확인', exact: true }).click();
    await settleSave(page);
    await expectVisible(page.getByRole('status').filter({ hasText: '정책표를 확인하지 못했습니다.' }));
    assert.equal(await input.inputValue(), 'https://www.notion.so/fixture-policy');
    const beforeRetry = await page.evaluate(() => {
        globalThis.__fixturePolicyTitle='복제한 기관 정책표';
        return globalThis.__fixtureConfigSaves.length;
    });
    await page.getByRole('button', { name: '저장하고 정책표 연결 확인', exact: true }).click();
    await settleSave(page);
    // Verification reads the real table title and persists it in a second write.
    // Do not pretend the first response also completed this separate request.
    await page.waitForFunction(count => globalThis.__fixtureConfigSaves.length === count + 2
        && globalThis.__fixtureHasPendingSave(), beforeRetry);
    await expectVisible(page.getByRole('dialog'));
    const normalized = await page.evaluate(() => JSON.parse(globalThis.__fixtureConfigSaves.at(-1).NOTION_POLICY_DBS));
    assert.equal(normalized[0].name, '복제한 기관 정책표');
    assert.equal(normalized[0].data_source_id, 'https://www.notion.so/fixture-policy');
    await settleSave(page);
    await expectHidden(page.getByRole('dialog'));
    const policyCard=page.getByRole('group',{name:'시작 안내 선택',exact:true})
        .locator('a[href="/settings?tab=guide&step=policy"]');
    await expectVisible(policyCard.getByText('✓ 설정 완료 · 다시 보기',{exact:true}));
    assert.deepEqual(await page.evaluate(() => JSON.parse(globalThis.__fixtureReadConfig().NOTION_POLICY_DBS)), normalized);
    assert.equal(await page.evaluate(() => globalThis.__fixtureSettingsDrafts.dirty('policyDbs')), false);
});

test('도우미를 완전히 벗어난 뒤 실패해도 새 화면에 알림을 띄우지 않고 초안을 유지한다', async t => {
    const page = await fixture(t, '정책');
    await keyInput(page).fill('fixture-unmounted-key');
    await fixtureControl(page, '다음 저장 실패');
    await saveKey(page).click();
    await fixtureControl(page, '다른 화면');
    await settleSave(page);
    await page.waitForFunction(() => !globalThis.__fixtureSettingsDrafts.isWriting());
    assert.equal(await page.getByText(/AI 연결 실패:/).count(), 0);
    await fixtureControl(page, '정책 단계 시험');
    await expectVisible(keyInput(page));
    assert.equal(await keyInput(page).inputValue(), 'fixture-unmounted-key');
});

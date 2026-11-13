import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';

async function fixture(mode, work) {
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    try {
        const page = await browser.newPage({ serviceWorkers: 'block' });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => { const url = new URL(route.request().url()); return url.origin === origin && !url.pathname.startsWith('/api/') ? route.continue() : route.abort(); });
        await page.goto(`${origin}/tests/ui/site-test-task.html?mode=${mode}`);
        const button = page.getByRole('button', { name: '시험 기관 수집 규칙 테스트', exact: true });
        await button.waitFor();
        await page.evaluate(() => window.siteTestFixture.notifyUnrelated());
        await page.getByText('별도 알림 유지', { exact: true }).waitFor();
        await work(page, button);
        assert.deepEqual(await page.evaluate(() => window.siteTestFixture.unexpected), []);
        assert.deepEqual(errors, []);
        assert.equal(await page.getByText('별도 알림 유지', { exact: true }).isVisible(), true);
    } finally { await browser.close(); }
}

for (const mode of ['slow', 'post-timeout', 'transient', 'malformed', 'zero', 'timeout', 'failed', 'verified', 'needs-check', 'idle', 'deleted']) {
    test(`사이트 점검 ${mode}: 작업 결과까지 복구하고 다른 알림을 보존한다`, async () => fixture(mode, async (page, button) => {
        await button.click();
        await page.waitForFunction(() => window.siteTestFixture.cachedTask()?.notified);
        const state = await page.evaluate(() => ({ ...window.siteTestFixture, task: window.siteTestFixture.cachedTask() }));
        assert.equal(state.posts, 1);
        assert.equal(await button.isEnabled(), true);
        if (mode === 'slow') assert.ok(state.elapsed >= 27000);
        const ending = state.notifications.filter(item => ['success', 'warning', 'error'].includes(item.level)).at(-1);
        assert.equal(ending.level, ['zero', 'needs-check'].includes(mode) ? 'warning' : ['timeout', 'failed', 'idle', 'deleted'].includes(mode) ? 'error' : 'success');
        if (['post-timeout', 'transient', 'malformed'].includes(mode)) assert.equal(state.notifications.some(item => item.level === 'error'), false);
    }));
}

test('두 번 클릭해도 POST는 한 번이며 점검 중 다른 화면에서 돌아오면 복원한다', async () => fixture('navigate', async (page, button) => {
    await button.evaluate(element => { element.click(); element.click(); });
    await page.waitForFunction(() => window.siteTestFixture.polls >= 1);
    assert.equal(await button.textContent().then(text => text.includes('점검 중')), true);
    await page.getByRole('link', { name: '다른 화면', exact: true }).click();
    await page.getByText('다른 작업 화면').waitFor();
    await page.evaluate(() => window.siteTestFixture.complete());
    await page.getByRole('link', { name: '사이트 화면', exact: true }).click();
    await page.waitForFunction(() => window.siteTestFixture.cachedTask()?.notified);
    assert.equal(await page.evaluate(() => window.siteTestFixture.posts), 1);
    assert.equal(await button.isEnabled(), true);
    await page.getByRole('link', { name: '다른 화면', exact: true }).click();
    await page.getByRole('link', { name: '사이트 화면', exact: true }).click();
    await button.waitFor();
    assert.equal(await page.evaluate(() => window.siteTestFixture.notifications.filter(item => item.level === 'success').length), 1);
}));

test('다른 작업의 409는 사이트 점검이라고 단정하지 않고 재시도를 허용한다', async () => fixture('conflict', async (page, button) => {
    await button.click();
    await page.getByText('다른 작업이 진행 중입니다. 잠시 후 다시 점검해주세요.', { exact: true }).waitFor();
    assert.equal(await button.isEnabled(), true);
    assert.equal(await page.evaluate(() => window.siteTestFixture.posts), 1);
    assert.equal(await page.evaluate(() => window.siteTestFixture.polls), 0);
}));

test('새 화면의 사이트 목록에서 진행 중 점검을 복원하며 POST는 하지 않는다', async () => fixture('recover', async (page, button) => {
    await page.waitForFunction(() => window.siteTestFixture.polls > 0);
    assert.equal(await button.isDisabled(), true);
    await page.evaluate(() => window.siteTestFixture.complete());
    await page.waitForFunction(() => window.siteTestFixture.cachedTask()?.notified);
    assert.equal(await page.evaluate(() => window.siteTestFixture.posts), 0);
}));

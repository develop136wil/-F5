import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1426';
async function fixture(t) {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    t.after(() => browser.close());
    const page = await browser.newPage({viewport: {width: 850, height: 700}});
    const errors = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
        unexpected.push(url.origin); return route.abort();
    });
    t.after(() => { assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); });
    await page.clock.install();
    await page.goto(origin + '/tests/ui/desktop-connection.html');
    await page.getByLabel('찾을 단어').waitFor();
    return page;
}
async function status(page, next) {
    await page.evaluate(next => { connectionFixture.next = next; }, next);
    await page.clock.runFor(1550);
}
test('brief transport delays preserve focus, caret, input and mount; recovery never resaves', async t => {
    const page = await fixture(t), input = page.getByLabel('찾을 단어');
    await input.fill('영유아 지원');
    await input.evaluate(el => el.setSelectionRange(2, 2));
    await status(page, {phase: 'starting', transient: true});
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await input.evaluate(el => document.activeElement === el && el.selectionStart === 2 && !el.closest('[inert]')), true);
    await page.keyboard.type(' 추가');
    const draft = await input.inputValue();
    await status(page, {phase: 'starting', transient: true});
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.getByRole('button', {name: '시험 저장'}).click();
    await status(page, {phase: 'ready', busy: false});
    assert.equal(await input.inputValue(), draft);
    assert.deepEqual(await page.evaluate(() => [connectionFixture.mounts, connectionFixture.saves]), [1, 1]);
});
test('third failure blocks actions, then restores focused draft without remount or save', async t => {
    const page = await fixture(t), input = page.getByLabel('찾을 단어');
    await input.fill('미저장 입력');
    for (let i = 0; i < 3; i++) await status(page, {phase: 'starting', transient: true});
    await page.getByRole('dialog', {name: '연결 상태를 확인하고 있어요'}).waitFor();
    assert.equal(await page.locator('input').evaluate(el => !!el.closest('[inert]')), true);
    await status(page, {phase: 'ready', busy: false});
    assert.equal(await input.inputValue(), '미저장 입력');
    assert.equal(await input.evaluate(el => el === document.activeElement), true);
    assert.deepEqual(await page.evaluate(() => [connectionFixture.mounts, connectionFixture.saves]), [1, 0]);
});
test('hard errors block immediately and a busy close still requires confirmation', async t => {
    const page = await fixture(t);
    await status(page, {phase: 'starting', transient: false, message: '인증 확인 필요'});
    assert.equal(await page.getByRole('dialog').count(), 1);
    await status(page, {phase: 'starting', transient: true});
    assert.equal(await page.getByRole('dialog').count(), 1);
    await status(page, {phase: 'ready', busy: true});
    await page.evaluate(() => {
        const f = connectionFixture;
        f.callbacks[f.events['desktop-close-requested']]({payload: 1});
    });
    const dialog = page.getByRole('dialog', {name: '작업을 마무리하고 종료할까요?'});
    await dialog.waitFor();
    await dialog.getByRole('button', {name: '창 유지하기'}).click();
    assert.equal(await page.evaluate(() => connectionFixture.calls.includes('desktop_shutdown')), false);
    assert.equal(await page.getByLabel('찾을 단어').isVisible(), true);
});

test('nonblocking notice fits a narrow dark screen and does not move scroll or focus', async t => {
    const page = await fixture(t);
    await page.setViewportSize({width: 390, height: 700});
    await page.evaluate(() => { document.documentElement.classList.add('dark'); window.scrollTo(0, 250); });
    const before = await page.evaluate(() => ({scroll: scrollY, focus: document.activeElement.tagName}));
    await status(page, {phase: 'starting', transient: true});
    const notice = page.getByRole('status');
    const box = await notice.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390);
    assert.equal(await notice.evaluate(el => getComputedStyle(el).pointerEvents), 'none');
    assert.deepEqual(await page.evaluate(() => ({scroll: scrollY, focus: document.activeElement.tagName})), before);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: fileURLToPath(new URL('../../../artifacts/connection-resilience-20260911/transient-notice-390.png', import.meta.url))});
    await status(page, {phase: 'ready', busy: false});
    assert.equal(await page.evaluate(() => scrollY), before.scroll);
});

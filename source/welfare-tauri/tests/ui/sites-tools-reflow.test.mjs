import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';
const output = '../artifacts/accessibility-20260908/sites-tools-fix';

test('사이트 파일 도구는 작은 화면과 글자 200%에서도 줄바꿈되고 키보드 파일선택은 한 번만 열린다', async t => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    await mkdir(output, {recursive: true});
    try {
        for (const width of [850, 582, 425]) for (const font of [100, 200]) {
            await t.test(`${width}px font${font}`, async () => {
                const page = await browser.newPage({viewport: {width, height: width === 425 ? 350 : 700}, serviceWorkers: 'block'});
                const errors = [], blocked = [], dialogs = [];
                page.on('pageerror', error => errors.push(error.message));
                page.on('dialog', async dialog => {dialogs.push(dialog.type()); await dialog.dismiss();});
                await page.route('**/*', route => {
                    const url = new URL(route.request().url());
                    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
                    blocked.push(url.pathname); return route.abort();
                });
                try {
                    await page.goto(`${origin}/tests/ui/accessibility-reflow.html?page=sites`);
                    await page.getByRole('heading', {name: '시험 복지 지원 기관', exact: true}).waitFor();
                    await page.evaluate(value => {document.documentElement.style.fontSize = `${value}%`;}, font);
                    const template = page.getByRole('button', {name: '주소 입력 양식 다운로드', exact: true});
                    const importer = page.getByRole('button', {name: '파일로 주소 등록', exact: true});
                    const controls = [];
                    for (const button of [page.getByRole('button', {name: '주소 등록', exact: true}), template, importer]) {
                        await button.scrollIntoViewIfNeeded(); await button.focus();
                        controls.push(await button.evaluate(element => {
                            const box = element.getBoundingClientRect(), main = element.closest('main').getBoundingClientRect();
                            return {text: element.textContent, left: box.left, right: box.right,
                                top: box.top, bottom: box.bottom, mainLeft: main.left, mainRight: main.right,
                                focused: element === document.activeElement, width: box.width,
                                scrollWidth: element.scrollWidth, clientWidth: element.clientWidth};
                        }));
                    }
                    const layout = await page.locator('main').evaluate(main => ({width: main.clientWidth,
                        scrollWidth: main.scrollWidth, rootFontSize: getComputedStyle(document.documentElement).fontSize}));
                    assert.ok(layout.scrollWidth <= layout.width + 2, JSON.stringify(layout));
                    for (const control of controls) {
                        assert.ok(control.left >= control.mainLeft - 2 && control.right <= control.mainRight + 2, JSON.stringify(control));
                        assert.ok(control.scrollWidth <= control.clientWidth + 2, JSON.stringify(control));
                        assert.equal(control.focused, true);
                    }
                    await template.focus(); await page.keyboard.press('Tab');
                    assert.equal(await importer.evaluate(element => element === document.activeElement), true);
                    const input = page.locator('#file-upload');
                    assert.equal(await input.getAttribute('aria-label'), '등록할 사이트 목록 파일');
                    assert.equal(await input.getAttribute('tabindex'), '-1');
                    assert.equal(await input.isVisible(), false);
                    await page.keyboard.press('Tab');
                    assert.equal(await input.evaluate(element => element === document.activeElement), false);
                    let opened = 0;
                    page.on('filechooser', () => {opened += 1;});
                    for (const key of ['Enter', 'Space']) {
                        await importer.focus();
                        const chosen = page.waitForEvent('filechooser');
                        await page.keyboard.press(key);
                        const chooser = await chosen;
                        assert.equal(chooser.isMultiple(), false);
                        await chooser.setFiles([]); // Cancel: no file read or import.
                    }
                    assert.equal(opened, 2);
                    assert.deepEqual(dialogs, []);
                    const fixture = await page.evaluate(() => window.a11yFixture);
                    assert.deepEqual(fixture.unexpected, []); assert.deepEqual(fixture.writes, []);
                    assert.deepEqual(errors, []); assert.deepEqual(blocked, []);
                    await importer.scrollIntoViewIfNeeded();
                    await page.screenshot({path: `${output}/sites-${width}-font${font}.png`, animations: 'disabled'});
                    await writeFile(`${output}/sites-${width}-font${font}.json`, JSON.stringify({
                        method: 'CSS viewport and html font size; not browser zoom', layout, controls,
                        chooserCount: opened, errors, blocked, writes: fixture.writes, unexpected: fixture.unexpected}, null, 2));
                } finally {await page.close();}
            });
        }
    } finally {await browser.close();}
});

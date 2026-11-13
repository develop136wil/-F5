import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, writeFile} from 'node:fs/promises';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';
const output = '../artifacts/accessibility-20260908/search-list-fix';

test('낮은 창·글자 200%에도 검색 목록이 접히지 않고 실제 카드 높이로 읽고 초점 이동할 수 있다', async t => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    await mkdir(output, {recursive: true});
    try {
        for (const width of [850, 582, 425]) for (const font of [100, 200]) {
            await t.test(`${width}px font${font}`, async () => {
                const page = await browser.newPage({viewport: {width, height: width === 425 ? 350 : 700}, serviceWorkers: 'block'});
                const errors = [], blocked = [];
                page.on('pageerror', error => errors.push(error.message));
                await page.route('**/*', route => {
                    const url = new URL(route.request().url());
                    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
                    blocked.push(url.pathname); return route.abort();
                });
                try {
                    await page.goto(`${origin}/tests/ui/accessibility-reflow.html?page=search`);
                    await page.getByRole('textbox', {name: '복지 정보 검색어', exact: true}).fill('지원');
                    await page.getByRole('button', {name: '검색', exact: true}).click();
                    const card = page.locator('main [role="button"][aria-label$="상세 정보 보기"]');
                    await card.waitFor();
                    await page.evaluate(value => {document.documentElement.style.fontSize = `${value}%`;}, font);
                    const input = page.getByRole('textbox', {name: '복지 정보 검색어', exact: true});
                    // Input has transition-all: inspect settled rem sizes, not
                    // an intermediate frame during the 200% font transition.
                    await page.waitForFunction(() => {
                        const root = parseFloat(getComputedStyle(document.documentElement).fontSize);
                        const input = getComputedStyle(document.getElementById('global-search-input'));
                        return Math.abs(parseFloat(input.fontSize) - root * 1.125) < 0.01
                            && Math.abs(parseFloat(input.paddingLeft) - root) < 0.01;
                    });
                    await input.focus();
                    const inputMetrics = await input.evaluate(element => {
                        const style = getComputedStyle(element), box = element.getBoundingClientRect();
                        const canvas = document.createElement('canvas'), context = canvas.getContext('2d');
                        context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                        return {clientWidth: element.clientWidth, paddingLeft: parseFloat(style.paddingLeft),
                            paddingRight: parseFloat(style.paddingRight), fontSize: parseFloat(style.fontSize),
                            visibleTextWidth: element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
                            valueWidth: context.measureText(element.value).width,
                            top: box.top, bottom: box.bottom, value: element.value};
                    });
                    assert.equal(inputMetrics.value, '지원');
                    assert.ok(inputMetrics.visibleTextWidth >= Math.max(inputMetrics.valueWidth + 2, inputMetrics.fontSize * 4), JSON.stringify(inputMetrics));
                    await page.keyboard.press('Tab');
                    assert.equal(await page.getByRole('button', {name: '검색', exact: true})
                        .evaluate(element => element === document.activeElement), true);
                    await input.scrollIntoViewIfNeeded();
                    await page.screenshot({path: `${output}/search-input-${width}-font${font}.png`, animations: 'disabled'});
                    await page.waitForFunction(() => {
                        const card = document.querySelector('main [data-index]');
                        const list = document.querySelector('main .scrollbar-thin');
                        return card && list && list.clientHeight >= 320 && Math.abs(
                            card.parentElement.getBoundingClientRect().height - card.getBoundingClientRect().height) < 2;
                    });
                    await card.scrollIntoViewIfNeeded(); await card.focus();
                    assert.equal(await card.evaluate(element => element === document.activeElement), true);
                    await page.keyboard.press('Tab');
                    const source = page.getByRole('link', {name: /원문 새 창에서 열기$/});
                    assert.equal(await source.evaluate(element => element === document.activeElement), true);
                    await source.scrollIntoViewIfNeeded();
                    const result = await page.locator('main').evaluate(main => {
                        const list = main.querySelector('.scrollbar-thin');
                        const row = list.querySelector('[data-index]');
                        const card = row.querySelector('[role="button"]');
                        const link = card.querySelector('a'), rect = link.getBoundingClientRect();
                        const hit = document.elementFromPoint((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2);
                        return {width: main.clientWidth, scrollWidth: main.scrollWidth,
                            rootFontSize: getComputedStyle(document.documentElement).fontSize,
                            listHeight: list.clientHeight, totalRowsHeight: row.parentElement.getBoundingClientRect().height,
                            rowHeight: row.getBoundingClientRect().height, cardHeight: card.getBoundingClientRect().height,
                            linkVisible: rect.right > 0 && rect.left < innerWidth && rect.bottom > 0 && rect.top < innerHeight,
                            linkRect: {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom},
                            linkUnobscured: Boolean(hit && (hit === link || link.contains(hit)))};
                    });
                    assert.ok(result.scrollWidth <= result.width + 2, JSON.stringify(result));
                    assert.ok(result.listHeight >= 320, JSON.stringify(result));
                    assert.ok(result.rowHeight >= result.cardHeight, JSON.stringify(result));
                    assert.equal(result.linkVisible, true);
                    assert.equal(result.linkUnobscured, true, JSON.stringify(result));
                    const fixture = await page.evaluate(() => window.a11yFixture);
                    assert.deepEqual(fixture.unexpected, []); assert.deepEqual(fixture.writes, []);
                    assert.deepEqual(errors, []); assert.deepEqual(blocked, []);
                    await page.screenshot({path: `${output}/search-${width}-font${font}.png`, animations: 'disabled'});
                    await writeFile(`${output}/search-${width}-font${font}.json`, JSON.stringify({
                        method: 'CSS viewport and html font size; not browser zoom', ...result, inputMetrics,
                        errors, blocked, writes: fixture.writes, unexpected: fixture.unexpected}, null, 2));
                } finally {await page.close();}
            });
        }
    } finally {await browser.close();}
});

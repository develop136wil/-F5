import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';
const output = '../artifacts/ux-pages-20260908';

async function setup(t, kind) {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    const page = await browser.newPage({viewport: {width: 582, height: 900}});
    const errors = [], blocked = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
        blocked.push(url.pathname);
        return route.abort();
    });
    t.after(async () => {
        try {
            assert.deepEqual(errors, []);
            assert.deepEqual(blocked, []);
            assert.deepEqual(await page.evaluate(() => pageUxFixture.unexpected), []);
        } finally { await browser.close(); }
    });
    await page.goto(`${origin}/tests/ui/page-ux.html?page=${kind}`);
    await page.getByRole('heading', {level: 1}).waitFor();
    await mkdir(output, {recursive: true});
    return page;
}

test('검색 안내는 입력 후에도 보이고 실패한 같은 검색을 키보드로 다시 시도할 수 있다', async t => {
    const page = await setup(t, 'search');
    const input = page.getByRole('textbox', {name: '복지 정보 검색어', exact: true});
    await input.fill('지원');
    await page.getByRole('button', {name: '검색', exact: true}).click();
    const retry = page.getByRole('button', {name: '검색 다시 시도', exact: true});
    await retry.waitFor();
    assert.equal(await page.locator('label[for="global-search-input"]').isVisible(), true);
    assert.equal(await input.getAttribute('aria-describedby'), null);
    assert.equal(await page.locator('#global-search-help').count(), 0);
    assert.equal(await page.getByRole('alert').count(), 1);
    for (const name of ['정확한 단어만', '통합 검색 (추천)']) {
        assert.equal(await page.getByRole('button', {name, exact: true})
            .evaluate(element => getComputedStyle(element).whiteSpace), 'nowrap');
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: `${output}/search-retry.png`, fullPage: true});
    await page.evaluate(() => { pageUxFixture.failSearch = false; });
    await retry.focus();
    await page.keyboard.press('Enter');
    await page.getByRole('heading', {name: '시험 복지 지원 소식', exact: true}).waitFor();
    assert.equal(await input.inputValue(), '지원');
    assert.deepEqual(await page.evaluate(() => pageUxFixture.calls), [
        {name: 'search', query: '지원'}, {name: 'search', query: '지원'}]);
    assert.equal(await page.getByRole('link', {name: '시험 복지 지원 소식 원문 새 창에서 열기'}).count(), 1);
});

test('사이트 주소 라벨과 여러 줄 안내는 입력 후에도 남고 주소를 등록하지 않는다', async t => {
    const page = await setup(t, 'sites');
    const input = page.getByRole('textbox', {name: '수집할 홈페이지 또는 게시판 주소', exact: true});
    const value = 'https://example.invalid/one\nhttps://example.invalid/two';
    await input.fill(value);
    assert.equal(await page.locator('label[for="site-registration-urls"]').isVisible(), true);
    assert.equal(await input.getAttribute('aria-describedby'), 'site-registration-help');
    assert.equal(await page.getByText('여러 주소는 한 줄에 하나씩 입력하세요.', {exact: true}).isVisible(), true);
    assert.equal(await input.inputValue(), value);
    assert.deepEqual(await page.evaluate(() => pageUxFixture.calls), []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: `${output}/site-address-label.png`, fullPage: true});
});

test('리포트 날짜 오류는 입력과 연결되고 기존 내보내기 차단을 유지하며 수정하면 사라진다', async t => {
    const page = await setup(t, 'reports');
    const start = page.getByLabel('수집일 (시작)', {exact: true});
    const end = page.getByLabel('수집일 (종료)', {exact: true});
    await start.fill('2026-10-31');
    await end.fill('2026-10-01');
    await page.locator('#report-date-error').waitFor();
    for (const input of [start, end]) {
        assert.equal(await input.getAttribute('aria-invalid'), 'true');
        assert.equal(await input.getAttribute('aria-describedby'), 'report-date-error');
    }
    await page.getByRole('button', {name: 'CSV로 받기', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => pageUxFixture.unexpected), []);
    assert.equal(await start.inputValue(), '2026-10-31');
    assert.equal(await end.inputValue(), '2026-10-01');
    await page.screenshot({path: `${output}/report-date-error.png`, fullPage: true});
    await end.fill('2026-11-01');
    await page.locator('#report-date-error').waitFor({state: 'detached'});
    assert.equal(await start.getAttribute('aria-invalid'), null);
    assert.equal(await end.getAttribute('aria-describedby'), null);
});

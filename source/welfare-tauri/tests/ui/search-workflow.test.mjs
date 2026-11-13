import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium} = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.UI_TEST_ORIGIN || 'http://127.0.0.1:1420';

async function fixture(t, viewport = {width: 1000, height: 850}) {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    const page = await browser.newPage({viewport, serviceWorkers: 'block'});
    const errors = [], blocked = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue();
        blocked.push(url.href); return route.abort();
    });
    t.after(async () => {
        try {
            assert.deepEqual(await page.evaluate(() => window.searchFixture.unexpected), []);
            assert.deepEqual(errors, []); assert.deepEqual(blocked, []);
        } finally {await page.close(); await browser.close();}
    });
    await page.goto(`${origin}/tests/ui/search-workflow.html`);
    await page.getByRole('textbox', {name: '복지 정보 검색어', exact: true}).waitFor();
    return page;
}
const cards = page => page.locator('main [role="button"][aria-label$="상세 정보 보기"]');
const list = page => page.locator('[aria-label="검색 결과 목록"]');
const search = async (page, value) => {
    await page.getByRole('textbox', {name: '복지 정보 검색어', exact: true}).fill(value);
    await page.getByRole('button', {name: '검색', exact: true}).click();
};
const exact = async page => {
    await page.getByRole('button', {name: '정확한 단어만', exact: true}).click();
    await search(page, '지원');
    await page.getByRole('button', {name: '단어 전체 지원 1번 상세 정보 보기', exact: true}).waitFor();
};
const pending = async page => {
    await page.waitForFunction(() => window.searchFixture.requests.at(-1)?.pending);
    return page.evaluate(() => window.searchFixture.requests.at(-1).id);
};
const resolve = (page, id) => page.evaluate(id => window.searchFixture.requests[id].resolve(), id);
const defer = page => page.evaluate(() => {window.searchFixture.next = 'defer';});
const open = async (page, title, id) => {
    const card = page.getByRole('button', {name: `${title} 상세 정보 보기`, exact: true});
    await card.focus(); await page.keyboard.press('Enter');
    await page.getByRole('dialog', {name: title, exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.searchFixture.details.at(-1)), id);
    await page.keyboard.press('Escape'); await page.getByRole('dialog').waitFor({state: 'detached'});
};

test('900px 검색 카드와 상세는 정리한 본문을 공유하고 빈 표시값에서 원문을 되살리지 않는다', async t => {
    const page = await fixture(t, {width:900,height:700});
    await page.evaluate(() => {window.searchFixture.displayCases=true;});
    await exact(page);
    for (const mode of ['단어','통합']) {
        if (mode === '통합') {
            await page.getByRole('button', {name:'통합 검색 (추천)',exact:true}).click();
            await page.getByRole('button', {name:'통합 전체 지원 1번 상세 정보 보기',exact:true}).waitFor();
        }
        const first = page.getByRole('button',{name:`${mode} 전체 지원 1번 상세 정보 보기`,exact:true});
        assert.match(await first.innerText(),/교재비 5만원\(50,000원\).*중복 신청은 불가/);
        assert.doesNotMatch(await first.innerText(),/###|\*\*|COPYRIGHT|첨부파일/);
        await first.click();
        const dialog = page.getByRole('dialog',{name:`${mode} 전체 지원 1번`,exact:true});
        await dialog.getByText('교재비 5만원(50,000원)',{exact:true}).waitFor();
        assert.doesNotMatch(await dialog.innerText(),/COPYRIGHT|숨겨야 하는 원문/);
        assert.equal(await dialog.locator('li').count(),2);
        await page.keyboard.press('Escape');
        await dialog.waitFor({state:'detached'});
        for (const ordinal of [2,3]) {
            const card = page.getByRole('button',{name:`${mode} 전체 지원 ${ordinal}번 상세 정보 보기`,exact:true});
            await card.scrollIntoViewIfNeeded();
            assert.match(await card.innerText(),/확인/);
            assert.doesNotMatch(await card.innerText(),/COPYRIGHT|첨부파일/);
        }
        const policy = page.getByRole('button',{name:`${mode} 전체 지원 4번 상세 정보 보기`,exact:true});
        await policy.scrollIntoViewIfNeeded();
        assert.match(await policy.innerText(),/담당자가 관리한 정책 본문 10만원/);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+2),true);
    }
});

test('다른 조건 대기 중 이전 카드·링크·숫자를 숨기고 새 결과만 키보드로 연다', async t => {
    const page = await fixture(t); await exact(page);
    await list(page).evaluate(el => {el.scrollTop = el.scrollHeight;});
    await page.getByRole('button', {name: '단어 전체 지원 100번 상세 정보 보기', exact: true}).waitFor();
    await defer(page); await page.getByRole('button', {name: '생활지원', exact: true}).click();
    const id = await pending(page);
    assert.equal(await page.getByRole('button', {name: '생활지원', exact: true}).getAttribute('aria-pressed'), 'true');
    assert.match(await page.getByRole('status').innerText(), /새 결과.*이전 조건·페이지/);
    assert.equal(await list(page).getAttribute('aria-busy'), 'true');
    assert.equal(await cards(page).count(), 0);
    assert.equal(await list(page).locator('a').count(), 0);
    assert.doesNotMatch(await page.locator('main').innerText(), /205|1 \/ 3/);
    assert.equal(await page.getByRole('button', {name: '다음', exact: true}).count(), 0);
    await page.keyboard.press('Tab');
    assert.equal(await list(page).evaluate(el => el.contains(document.activeElement)), false);
    assert.deepEqual(await page.evaluate(() => window.searchFixture.details), []);
    await resolve(page, id);
    await page.getByRole('button', {name: '단어 생활지원 지원 1번 상세 정보 보기', exact: true}).waitFor();
    assert.equal(await list(page).evaluate(el => el.scrollTop), 0);
    await open(page, '단어 생활지원 지원 1번', 1001);
    assert.equal(await page.getByRole('link', {name: '단어 생활지원 지원 1번 원문 새 창에서 열기', exact: true}).getAttribute('href'), 'https://example.invalid/news/1001');
});

test('100건 가상 목록의 마지막 항목·다음 페이지·캐시된 이전 페이지 ID와 스크롤이 맞는다', async t => {
    const page = await fixture(t); await exact(page);
    await list(page).evaluate(el => {el.scrollTop = el.scrollHeight;});
    await page.getByRole('button', {name: '단어 전체 지원 100번 상세 정보 보기', exact: true}).waitFor();
    assert.ok(await cards(page).count() < 100);
    await open(page, '단어 전체 지원 100번', 100);
    await defer(page); await page.getByRole('button', {name: '다음', exact: true}).click();
    const id = await pending(page);
    assert.equal(await list(page).evaluate(el => el.scrollTop), 0);
    assert.doesNotMatch(await page.locator('main').innerText(), /2 \/ 3/);
    await resolve(page, id);
    await page.getByRole('button', {name: '단어 전체 지원 101번 상세 정보 보기', exact: true}).waitFor();
    await open(page, '단어 전체 지원 101번', 101);
    await list(page).evaluate(el => {el.scrollTop = el.scrollHeight;});
    await page.getByRole('button', {name: '단어 전체 지원 200번 상세 정보 보기', exact: true}).waitFor();
    await open(page, '단어 전체 지원 200번', 200);
    await page.getByRole('button', {name: '다음', exact: true}).click();
    await page.getByRole('button', {name: '단어 전체 지원 201번 상세 정보 보기', exact: true}).waitFor();
    await open(page, '단어 전체 지원 205번', 205);
    assert.equal(await page.getByRole('button', {name: '다음', exact: true}).isDisabled(), true);
    await page.getByRole('button', {name: '이전', exact: true}).click();
    await page.getByRole('button', {name: '단어 전체 지원 101번 상세 정보 보기', exact: true}).waitFor();
    assert.equal(await list(page).evaluate(el => el.scrollTop), 0);
    assert.deepEqual(await page.evaluate(() => window.searchFixture.details), [100, 101, 200, 205]);
});

test('검색어의 늦은 응답·분류·활성 필터·통합/단어 전환은 현재 조건과 ID를 유지한다', async t => {
    const page = await fixture(t); await exact(page);
    await list(page).evaluate(el => {el.scrollTop = el.scrollHeight;});
    await page.getByRole('button', {name: '단어 전체 지원 100번 상세 정보 보기', exact: true}).waitFor();
    await defer(page); await search(page, '먼저검색'); const older = await pending(page);
    await defer(page); await search(page, '새검색'); const current = await pending(page);
    await resolve(page, older);
    assert.equal(await cards(page).count(), 0);
    await resolve(page, current);
    await page.getByRole('button', {name: '단어 전체 새검색 1번 상세 정보 보기', exact: true}).waitFor();
    assert.equal(await list(page).evaluate(el => el.scrollTop), 0);
    await open(page, '단어 전체 새검색 1번', 2001);
    await defer(page); await page.getByRole('button', {name: '통합 검색 (추천)', exact: true}).click();
    const semantic = await pending(page); assert.equal(await cards(page).count(), 0);
    await resolve(page, semantic);
    await page.getByRole('button', {name: '통합 전체 새검색 1번 상세 정보 보기', exact: true}).waitFor();
    await open(page, '통합 전체 새검색 1번', 12001);
    await search(page, '');
    await page.getByText('검색어를 입력하세요 (최소 2자).', {exact: true}).waitFor();
    assert.equal(await cards(page).count(), 0);
    await page.getByRole('button', {name: '교육·보육', exact: true}).click();
    await page.getByRole('button', {name: '단어 교육·보육 모든자료 1번 상세 정보 보기', exact: true}).waitFor();
    await page.getByRole('button', {name: '마감된 항목 제외', exact: true}).click();
    await page.waitForFunction(() => window.searchFixture.requests.at(-1)?.activeOnly === true);
    await open(page, '단어 교육·보육 모든자료 1번', 5001);
    const request = await page.evaluate(() => window.searchFixture.requests.at(-1));
    assert.equal(request.mode, 'exact'); assert.equal(request.query, ''); assert.equal(request.category, '교육·보육'); assert.equal(request.offset, 0);
});

test('검색 오류 재시도는 같은 조건이고 같은 조건 갱신은 기존 결과를 구분해 유지한다', async t => {
    const page = await fixture(t);
    await page.evaluate(() => {window.searchFixture.next = 'fail';}); await search(page, '지원');
    await page.getByRole('alert').waitFor();
    assert.equal(await cards(page).count(), 0);
    const initial = await page.evaluate(() => window.searchFixture.requests.at(-1));
    await page.getByRole('button', {name: '검색 다시 시도', exact: true}).focus(); await page.keyboard.press('Enter');
    await page.getByRole('button', {name: '통합 전체 지원 1번 상세 정보 보기', exact: true}).waitFor();
    const retried = await page.evaluate(() => window.searchFixture.requests.at(-1));
    for (const key of ['mode', 'query', 'category', 'offset', 'activeOnly']) assert.equal(retried[key], initial[key]);
    assert.match(await page.locator('main').innerText(), /140[\s\S]*100/);
    await defer(page); await page.evaluate(() => {void window.searchFixture.refresh();});
    const refresh = await pending(page);
    assert.match(await page.getByRole('status').innerText(), /같은 조건으로 다시 확인 중/);
    assert.ok(await cards(page).count() > 0);
    await open(page, '통합 전체 지원 1번', 10001);
    await page.evaluate(id => window.searchFixture.requests[id].reject(), refresh);
    await page.getByRole('alert').waitFor();
    assert.match(await page.getByRole('alert').innerText(), /이전에 찾은 결과/);
    assert.ok(await cards(page).count() > 0);
    await page.getByRole('button', {name: '검색 다시 시도', exact: true}).click();
    await page.getByRole('alert').waitFor({state: 'detached'});
    await page.evaluate(() => {window.searchFixture.next = 'fail';});
    await page.getByRole('button', {name: '정확한 단어만', exact: true}).click();
    await page.getByRole('alert').waitFor();
    assert.equal(await cards(page).count(), 0);
    await page.getByRole('button', {name: '검색 다시 시도', exact: true}).click();
    await page.getByRole('button', {name: '단어 전체 지원 1번 상세 정보 보기', exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.searchFixture.requests.at(-1).mode), 'exact');
});

test('582px·글자 200%의 여러 높이 100건에서도 겹침 없이 마지막 결과를 키보드로 연다', async t => {
    const page = await fixture(t, {width: 582, height: 700});
    await page.evaluate(() => {document.documentElement.style.fontSize = '200%';});
    await exact(page);
    for (const fraction of [0, 0.5, 1]) {
        await list(page).evaluate((el, fraction) => {el.scrollTop = (el.scrollHeight - el.clientHeight) * fraction;}, fraction);
        await page.waitForFunction(() => {
            const rows = [...document.querySelectorAll('[data-index]')];
            return rows.length > 1 && rows.every((row, i) => !i || row.getBoundingClientRect().top >= rows[i - 1].getBoundingClientRect().bottom - 1);
        });
        assert.ok(await cards(page).count() < 100);
    }
    const last = page.getByRole('button', {name: '단어 전체 지원 100번 상세 정보 보기', exact: true});
    await last.waitFor(); await last.scrollIntoViewIfNeeded(); await last.focus();
    await page.keyboard.press('Tab');
    const link = page.getByRole('link', {name: '단어 전체 지원 100번 원문 새 창에서 열기', exact: true});
    assert.equal(await link.evaluate(el => el === document.activeElement), true);
    assert.equal(await link.getAttribute('href'), 'https://example.invalid/news/100');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await last.evaluate(el => el === document.activeElement), true);
    await open(page, '단어 전체 지원 100번', 100);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true);
});

test('검색어 교체는 단어·통합 검색의 이전 요청을 취소하고 취소 오류를 표시하지 않는다', async t => {
    for (const mode of ['exact', 'semantic']) {
        const page = await fixture(t);
        if (mode === 'exact') await exact(page);
        else {await search(page, '지원'); await cards(page).first().waitFor();}
        await defer(page); await search(page, '먼저검색'); const older = await pending(page);
        await defer(page); await search(page, '새검색'); const current = await pending(page);
        await page.waitForFunction(id => window.searchFixture.aborted.includes(id), older);
        const requests = await page.evaluate(() => window.searchFixture.requests);
        assert.equal(requests[older].hasSignal, true); assert.equal(requests[current].hasSignal, true);
        assert.equal(requests[older].aborted, true); assert.notEqual(requests[current].aborted, true);
        assert.equal(await page.getByRole('alert').count(), 0);
        assert.equal(await cards(page).count(), 0);
        await resolve(page, current);
        const title = `${mode === 'exact' ? '단어' : '통합'} 전체 새검색 1번`;
        await page.getByRole('button', {name: `${title} 상세 정보 보기`, exact: true}).waitFor();
        await open(page, title, mode === 'exact' ? 2001 : 12001);
        assert.equal(await page.getByRole('alert').count(), 0);
    }
});

test('검색 화면 종료는 대기 중인 두 검색 방식의 요청을 취소한다', async t => {
    for (const mode of ['exact', 'semantic']) {
        const page = await fixture(t);
        if (mode === 'exact') await exact(page);
        else {await search(page, '지원'); await cards(page).first().waitFor();}
        await defer(page); await search(page, '먼저검색'); const current = await pending(page);
        await page.evaluate(() => window.searchFixture.unmount());
        await page.waitForFunction(id => window.searchFixture.aborted.includes(id), current);
        assert.equal(await page.locator('main').count(), 0);
        assert.equal(await page.evaluate(id => window.searchFixture.requests[id].pending, current), false);
        assert.equal(await page.evaluate(id => window.searchFixture.requests[id].hasSignal, current), true);
        assert.deepEqual(await page.evaluate(() => window.searchFixture.details), []);
    }
});

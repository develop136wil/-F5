import assert from 'node:assert/strict';
import test from 'node:test';
import { downloadBlob } from '../src/utils/download.js';

test('다운로드 클릭 뒤 URL은 지연 정리하고 클릭 실패 시에는 즉시 정리한다', async t => {
    const previousWindow = globalThis.window, previousDocument = globalThis.document;
    const events = [];
    let cleanup, throwOnClick = false;
    const link = { setAttribute() {}, remove() { events.push('remove'); }, click() {
        events.push('click'); if (throwOnClick) throw Error('다운로드 차단');
    }};
    globalThis.window = {URL: {createObjectURL: () => 'blob:test', revokeObjectURL: () => events.push('revoke')}};
    globalThis.document = {createElement: () => link, body: {appendChild: () => events.push('append')}};
    t.mock.method(globalThis, 'setTimeout', (fn, delay) => { assert.equal(delay, 30_000); cleanup = fn; });
    try {
        downloadBlob('테스트', 'test.html', 'text/html');
        assert.deepEqual(events, ['append', 'click', 'remove']);
        cleanup();
        assert.equal(events.at(-1), 'revoke');
        events.length = 0;
        throwOnClick = true;
        assert.throws(() => downloadBlob('테스트', 'test.html'), /차단/);
        assert.deepEqual(events, ['append', 'click', 'revoke', 'remove']);
    } finally {
        if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow;
        if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
    }
});

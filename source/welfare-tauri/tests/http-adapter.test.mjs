import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

// 실제 어댑터 함수를 실행하되 원격 네트워크 대신 fetch만 주입한다.
const source = fs.readFileSync(new URL('../src/api/client.js', import.meta.url), 'utf8');
const body = source.slice(source.indexOf('const tauriAdapter ='), source.indexOf('// Create the axios instance'));
const adapter = fetch => new Function('getTauriFetch', body + ';return tauriAdapter;')(async () => fetch);
const response = (read) => ({ ok: true, status: 200, headers: { get: () => 'application/json' }, json: read });

test('본문 읽기까지 시간 제한 적용', async () => {
    const run = adapter(async () => response(() => new Promise(() => {})));
    await assert.rejects(run({ url: 'http://test.invalid', timeout: 10 }), { code: 'ECONNABORTED' });
});
test('JSON 손상을 정상 성공으로 숨기지 않음', async () => {
    const run = adapter(async () => response(async () => { throw new SyntaxError('bad json'); }));
    await assert.rejects(run({ url: 'http://test.invalid', timeout: 100 }), SyntaxError);
});
test('호출자의 취소 신호를 전달', async () => {
    const abort = new AbortController();
    const run = adapter(async () => { queueMicrotask(() => abort.abort()); return response(() => new Promise(() => {})); });
    await assert.rejects(run({ url: 'http://test.invalid', signal: abort.signal }), { code: 'ERR_CANCELED' });
});
test('이미 취소된 요청은 전송하지 않음', async () => {
    const abort = new AbortController(); abort.abort();
    const run = adapter(async () => { assert.fail('must not fetch'); });
    await assert.rejects(run({ url: 'http://test.invalid', signal: abort.signal }), { code: 'ERR_CANCELED' });
});
test('FormData와 Blob 바이트 보존', async () => {
    const data = new FormData(); data.append('a', '한글');
    const bytes = new Uint8Array([0, 255, 128]);
    const run = adapter(async (_, options) => {
        assert.equal(options.body, data);
        assert.equal(options.headers['Content-Type'], undefined);
        return { ok: true, status: 200, blob: async () => new Blob([bytes]) };
    });
    const result = await run({ url: 'http://test.invalid', method: 'post', data,
        headers: { 'Content-Type': 'multipart/form-data' }, responseType: 'blob' });
    assert.deepEqual(new Uint8Array(await result.data.arrayBuffer()), bytes);
});

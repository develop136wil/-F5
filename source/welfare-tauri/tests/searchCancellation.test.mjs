import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// Execute only the actual two search methods with an injected client. Neither
// the production client nor any real HTTP transport is imported by these tests.
const source = readFileSync(new URL('../src/api/welfare.js', import.meta.url), 'utf8');
const methods = source.slice(source.indexOf('    searchItems: async'), source.indexOf('    // Embedding backfill'));
const apiWith = client => new Function('client', `return {${methods}};`)(client);
const adapterSource = readFileSync(new URL('../src/api/client.js', import.meta.url), 'utf8');
const adapterBody = adapterSource.slice(adapterSource.indexOf('const tauriAdapter ='), adapterSource.indexOf('// Create the axios instance'));
const adapterWith = fetch => new Function('getTauriFetch', `${adapterBody};return tauriAdapter;`)(async () => fetch);

test('기존 검색 호출자는 취소 인자 없이도 같은 기본 조건과 결과를 받는다', async () => {
    const calls = [], result = {items: [], total: 0};
    const api = apiWith({get: async (url, config) => {calls.push({url, config}); return {data: result};}});
    assert.equal(await api.searchItems('지원'), result);
    assert.deepEqual(calls[0], {url: '/search', config: {
        params: {q: '지원', category: undefined, status: undefined, limit: 20, offset: 0, active_only: false}, signal: undefined,
    }});
    assert.equal(await api.semanticSearch('지원'), result);
    assert.deepEqual(calls[1], {url: '/search/semantic', config: {
        params: {q: '지원', category: undefined, limit: 20, min_score: 0.55, active_only: false, include_archived: false},
        timeout: 30000, signal: undefined,
    }});
});

test('두 검색 방식은 동일한 AbortSignal을 HTTP 설정에만 전달하고 검색 조건을 보존한다', async () => {
    const calls = [], controller = new AbortController();
    const api = apiWith({get: async (url, config) => {calls.push({url, config}); return {data: []};}});
    await api.searchItems('지원', '교육·보육', '진행 중', 100, 200, true, controller.signal);
    await api.semanticSearch('지원', {category: '생활지원', limit: 50, minScore: 0.7, activeOnly: true, includeArchived: true, signal: controller.signal});
    assert.equal(calls[0].config.signal, controller.signal); assert.equal(calls[1].config.signal, controller.signal);
    assert.deepEqual(calls[0].config.params, {q: '지원', category: '교육·보육', status: '진행 중', limit: 100, offset: 200, active_only: true});
    assert.deepEqual(calls[1].config.params, {q: '지원', category: '생활지원', limit: 50, min_score: 0.7, active_only: true, include_archived: true});
    assert.equal(calls[1].config.timeout, 30000);
});

test('검색 API에서 native adapter까지 취소를 전달하고 ERR_CANCELED를 그대로 반환한다', async () => {
    for (const mode of ['exact', 'semantic']) {
        const controller = new AbortController(); let transportSignal, entered;
        const started = new Promise(resolve => {entered = resolve;});
        const adapter = adapterWith(async (_url, options) => {
            transportSignal = options.signal; entered();
            return new Promise(() => {}); // Synthetic transport; no sockets.
        });
        const api = apiWith({get: (url, config) => adapter({...config, url, baseURL: 'http://example.invalid/api'})});
        const task = mode === 'exact' ? api.searchItems('지원', undefined, undefined, 100, 0, false, controller.signal)
            : api.semanticSearch('지원', {signal: controller.signal});
        const rejected = assert.rejects(task, {code: 'ERR_CANCELED'});
        await started; controller.abort(); await rejected;
        assert.equal(transportSignal.aborted, true);
    }
});

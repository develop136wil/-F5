import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {scrapeCompletionNotice} from '../src/utils/scrapeCompletion.js';

// Provider의 실제 effect 코드를 실행한다. DOM 렌더링/E2E 테스트와는 구분한다.
const source = fs.readFileSync(new URL('../src/context/ScrapeContext.jsx', import.meta.url), 'utf8');
const start = source.indexOf('export const ScrapeProvider = ({ children }) => {') + 'export const ScrapeProvider = ({ children }) => {'.length;
const body = source.slice(start, source.indexOf('    return (\n        <ScrapeContext.Provider', start));

function mount(getStatus, running = false) {
    const effects = [], timers = [], states = [], notices = [], notifications = [];
    let stateIndex = 0;
    const env = {
        useState: initial => [stateIndex++ === 0 ? running : initial, value => states.push(value)],
        useRef: value => ({ current: value }), useCallback: fn => fn,
        useEffect: fn => effects.push(fn),
        useQueryClient: () => ({ invalidateQueries() {} }),
        welfareApi: { getStatus }, queryKeys: {recentItems:()=>[],trends:()=>[]},
        scrapeCompletionNotice,
        toast: Object.fromEntries(['warning','error','success'].map(tone=>[tone,message=>notices.push({tone,message})])),
        isPermissionGranted: async()=>true, requestPermission:async()=>'granted',sendNotification:value=>notifications.push(value),
        setTimeout: (fn, delay) => { const timer = { fn, delay }; timers.push(timer); return timer; },
        clearTimeout: timer => { if (timer) timer.cancelled = true; },
    };
    new Function(...Object.keys(env), body)(...Object.values(env));
    return { cleanup: effects[0](), timers, states, notices, notifications };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('유휴 화면에서도 예약 작업 시작을 감지', async () => {
    let count = 0;
    const mounted = mount(async () => ({ is_running: ++count > 1, progress: 20 }));
    await flush();
    assert.equal(mounted.timers.length, 1);
    // 유휴 중에는 서버 부하를 줄이되 예약 실행을 체감 가능한 10초 안에 감지한다.
    assert.equal(mounted.timers[0].delay, 10000);
    await mounted.timers.shift().fn();
    assert.ok(mounted.states.includes(true));
    mounted.cleanup();
});

test('느린 조회 중 새 타이머를 만들지 않고 정리 시 취소 신호 전달', async () => {
    let finish, signal, count = 0;
    const mounted = mount(s => { signal = s; count++; return new Promise(resolve => { finish = resolve; }); });
    await flush();
    assert.equal(count, 1);
    assert.equal(mounted.timers.length, 0);
    mounted.cleanup();
    assert.equal(signal.aborted, true);
    finish({ is_running: true });
    await flush();
    assert.equal(mounted.timers.length, 0);
    assert.equal(mounted.states.length, 0);
});

test('부분 분석 실패는 경고로 남기고 완료 알림을 보내지 않는다', async () => {
    const mounted=mount(async()=>({is_running:false,status_code:'partial_failure',message:'분석 1건을 마치지 못했습니다.',ai_savings:{analysis_failed:1}}),true);
    await flush();
    assert.deepEqual(mounted.notices,[{tone:'warning',message:'분석 1건을 마치지 못했습니다.'}]);
    assert.equal(mounted.notifications.length,0);
    assert.ok(mounted.states.some(value=>value?.tone==='warning'));
    mounted.cleanup();
});

test('화면을 다시 열어도 마지막 실패 이유를 표시하되 반복 토스트는 띄우지 않는다', async () => {
    const mounted=mount(async()=>({is_running:false,status_code:'partial_failure',message:'분석 대기 1건'}));
    await flush();
    assert.ok(mounted.states.some(value=>value?.message==='분석 대기 1건'));
    assert.equal(mounted.notices.length,0);
    mounted.cleanup();
});

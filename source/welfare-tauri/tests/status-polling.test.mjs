import test from 'node:test';
import assert from 'node:assert/strict';
import { startStatusPolling } from '../src/utils/statusPolling.js';

const flush = () => new Promise(resolve => setImmediate(resolve));

const fakeTimers = () => {
    const timers = [];
    return {
        timers,
        schedule(fn, delay) {
            const timer = { fn, delay, cancelled: false };
            timers.push(timer);
            return timer;
        },
        cancel(timer) { timer.cancelled = true; },
    };
};

test('느린 상태 조회가 끝나기 전에는 다음 요청을 예약하지 않음', async () => {
    const clock = fakeTimers();
    let calls = 0;
    let resolveRequest;
    const stop = startStatusPolling({
        fetchStatus: () => {
            calls += 1;
            return new Promise(resolve => { resolveRequest = resolve; });
        },
        schedule: clock.schedule,
        cancel: clock.cancel,
        isBackground: () => false,
    });

    assert.equal(clock.timers.length, 1);
    assert.equal(clock.timers[0].delay, 1500);
    const firstRun = clock.timers.shift().fn();
    await flush();
    assert.equal(calls, 1);
    assert.equal(clock.timers.length, 0);

    resolveRequest({ running: true, checked: 1 });
    await firstRun;
    assert.equal(clock.timers.length, 1);
    assert.equal(clock.timers[0].delay, 1500);
    stop();
});

test('작업 완료를 받으면 상태를 전달하고 추가 조회를 중단함', async () => {
    const clock = fakeTimers();
    const statuses = [];
    const completed = [];
    startStatusPolling({
        fetchStatus: async () => ({ running: false, message: '완료' }),
        onStatus: status => statuses.push(status),
        onComplete: status => completed.push(status),
        schedule: clock.schedule,
        cancel: clock.cancel,
        isBackground: () => false,
    });

    await clock.timers.shift().fn();
    assert.equal(statuses.length, 1);
    assert.equal(completed.length, 1);
    assert.equal(completed[0].message, '완료');
    assert.equal(clock.timers.length, 0);
});

test('불완전한 성공 응답은 마지막 진행 상태를 유지하고 재조회한다', async () => {
    for (const invalid of [null, undefined, {}, [], 'completed', false, 0,
        { running: 'false' }, { running: 0 }, { running: null }]) {
        const clock = fakeTimers();
        const running = { running: true, checked: 2, total: 5 };
        const complete = { running: false, message: '완료' };
        const responses = [running, invalid, complete];
        const statuses = [], completed = [], errors = [];
        startStatusPolling({
            fetchStatus: async () => responses.shift(),
            onStatus: status => statuses.push(status),
            onComplete: status => completed.push(status),
            onError: error => errors.push(error),
            schedule: clock.schedule,
            cancel: clock.cancel,
            isBackground: () => false,
        });
        await clock.timers.shift().fn();
        await clock.timers.shift().fn();
        assert.deepEqual(statuses, [running]);
        assert.deepEqual(completed, []);
        assert.equal(errors.length, 1);
        assert.match(errors[0].message, /진행 상태/);
        assert.equal(clock.timers.length, 1);
        await clock.timers.shift().fn();
        assert.deepEqual(statuses, [running, complete]);
        assert.deepEqual(completed, [complete]);
        assert.equal(clock.timers.length, 0);
    }
});

test('상태 조회 실패 후에도 진행과 완료를 계속 확인한다', async () => {
    const clock = fakeTimers();
    const statuses = [], completed = [], errors = [];
    let requests = 0;
    startStatusPolling({
        fetchStatus: async () => {
            if (++requests === 1) throw new Error('synthetic connection failure');
            return { running: requests === 2 };
        },
        onStatus: status => statuses.push(status),
        onComplete: status => completed.push(status),
        onError: error => errors.push(error),
        schedule: clock.schedule,
        cancel: clock.cancel,
        isBackground: () => false,
    });
    await clock.timers.shift().fn();
    assert.equal(errors.length, 1);
    assert.deepEqual(statuses, []);
    await clock.timers.shift().fn();
    await clock.timers.shift().fn();
    assert.deepEqual(statuses, [{ running: true }, { running: false }]);
    assert.deepEqual(completed, [{ running: false }]);
    assert.equal(clock.timers.length, 0);
});

test('화면이 백그라운드이면 상태 조회 간격을 늘림', () => {
    const clock = fakeTimers();
    const stop = startStatusPolling({
        fetchStatus: async () => ({ running: true }),
        schedule: clock.schedule,
        cancel: clock.cancel,
        isBackground: () => true,
    });
    assert.equal(clock.timers[0].delay, 5000);
    stop();
    assert.equal(clock.timers[0].cancelled, true);
});

test('화면 이탈 시 진행 중 요청을 취소하고 완료 콜백을 실행하지 않음', async () => {
    const clock = fakeTimers();
    let requestSignal;
    let resolveRequest;
    let completed = 0;
    const stop = startStatusPolling({
        fetchStatus: signal => {
            requestSignal = signal;
            return new Promise(resolve => { resolveRequest = resolve; });
        },
        onComplete: () => { completed += 1; },
        schedule: clock.schedule,
        cancel: clock.cancel,
        isBackground: () => false,
    });

    const running = clock.timers.shift().fn();
    await flush();
    stop();
    assert.equal(requestSignal.aborted, true);
    resolveRequest({ running: false });
    await running;
    assert.equal(completed, 0);
    assert.equal(clock.timers.length, 0);
});

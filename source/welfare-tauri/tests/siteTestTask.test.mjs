import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { validateSiteTestTask, siteTestOutcome } from '../src/utils/siteTestTask.js';

const snapshot = (status, result = null) => ({ site_id: 7, task_id: 'test-7', status, running: status === 'running', result });

test('사이트 점검은 실행/완료/실제 실패 상태를 구분한다', () => {
    for (const status of ['running', 'completed', 'failed', 'timed_out', 'cancelled']) {
        const task = snapshot(status, status === 'completed' ? { count: 0 } : null);
        assert.equal(validateSiteTestTask(task, 7), task);
    }
    assert.equal(validateSiteTestTask({ site_id: 7, task_id: null, status: 'idle', running: false }, 7).status, 'idle');
});

test('잘못된 상태 응답은 성공/실패로 처리하지 않고 다시 조회한다', () => {
    for (const value of [null, [], {}, { ...snapshot('running'), site_id: 8 },
        { ...snapshot('running'), running: false }, { ...snapshot('running'), task_id: null },
        snapshot('unknown'), snapshot('completed')]) {
        assert.throws(() => validateSiteTestTask(value, 7));
    }
});

test('fallback 성공은 항목과 본문 검증 결과로 안내하며 0건과 구분한다', () => {
    assert.equal(siteTestOutcome(snapshot('completed', { site: '시험 기관', count: 10, items: [{ title: '시험 소식' }] })).level, 'success');
    assert.match(siteTestOutcome(snapshot('completed', { count: 10 })).message, /10개/);
    assert.equal(siteTestOutcome(snapshot('completed', { count: 0, items: [] })).level, 'warning');
    assert.equal(siteTestOutcome(snapshot('completed', { registration_status: 'basic_verified' })).level, 'success');
    assert.match(siteTestOutcome(snapshot('completed', { registration_status: 'needs_check' })).message, /주소는 수집 대상으로 유지/);
});

test('실제 제한시간 종료와 실패는 재시도 가능한 안내를 유지한다', () => {
    assert.match(siteTestOutcome(snapshot('timed_out')).message, /응답이 늦어/);
    assert.equal(siteTestOutcome({ ...snapshot('failed'), message: '연결 실패' }).message, '연결 실패');
    assert.equal(siteTestOutcome(snapshot('cancelled')).level, 'error');
});

test('사이트 점검 상태 조회는 읽기 전용이며 숨은 자동 재시도와 전체 토스트 삭제가 없다', async () => {
    const api = await readFile(new URL('../src/api/welfare.js', import.meta.url), 'utf8');
    const hook = await readFile(new URL('../src/hooks/useSiteTest.js', import.meta.url), 'utf8');
    const sites = await readFile(new URL('../src/pages/Sites.jsx', import.meta.url), 'utf8');
    assert.match(api, /client\.get\(`\/sites\/\$\{siteId\}\/test-status`, \{ signal, retry: false \}\)/);
    assert.doesNotMatch(hook + sites, /toast\.dismiss\(\)/);
    assert.equal((hook.match(/welfareApi\.testSite\(/g) || []).length, 1);
    assert.match(hook, /gcTime: Infinity/);
});

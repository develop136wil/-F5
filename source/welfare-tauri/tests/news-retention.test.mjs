import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {newsRetentionDays, newsRetentionConfirmation} from '../src/utils/newsRetention.js';

test('보관 설정은 명시적인 끄기·7일·30일만 허용한다', () => {
    for (const days of [0, 7, 30]) assert.equal(newsRetentionDays(days),days);
    for (const days of [null, undefined, true, false, '7', -1, 1, 365, NaN]) assert.throws(() => newsRetentionDays(days));
});
test('정리 동의는 기존 미채택 기록 삭제와 보호 범위를 구분한다', () => {
    assert.equal(newsRetentionConfirmation(0),null);
    for (const days of [7,30]) {
        const prompt = newsRetentionConfirmation(days);
        assert.match(prompt,new RegExp(`${days}일`));
        assert.match(prompt,/기존 분석 실패·보류·제외/);
        assert.match(prompt,/되돌릴 수 없습니다/);
        assert.match(prompt,/찾은 소식, 저장한 자료, Notion 원본과 진행 중인 작업은 유지/);
    }
});
test('기록 정리는 저장 동작에서만 시작하며 조회 실패는 저장을 차단한다', () => {
    const text=readFileSync(new URL('../src/components/NewsRecordRetentionSettings.jsx',import.meta.url),'utf8');
    assert.match(text,/if \(!loaded \|\| pending.current\) return/);
    assert.match(text,/if \(question && !window.confirm\(question\)\) return/);
    assert.match(text,/settingsDrafts.runWrite/);
    assert.match(text,/settingsDrafts.commit\('newsRecordRetentionDays'/);
    assert.match(text,/cleanup_failed/);
    assert.equal((text.match(/saveNewsRecordRetention\(/g)||[]).length,1);
});

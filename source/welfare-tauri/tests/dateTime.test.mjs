import test from 'node:test';
import assert from 'node:assert/strict';
import { formatStoredUtcTime } from '../src/utils/dateTime.js';

test('SQLite 시간대 없는 CURRENT_TIMESTAMP를 UTC로 해석', () => {
    assert.equal(
        formatStoredUtcTime('2026-09-01 04:37:00'),
        formatStoredUtcTime('2026-09-01T04:37:00Z'),
    );
});

test('명시된 시간대와 빈 값을 안전하게 처리', () => {
    assert.equal(
        formatStoredUtcTime('2026-09-01T13:37:00+09:00'),
        formatStoredUtcTime('2026-09-01T04:37:00Z'),
    );
    assert.equal(formatStoredUtcTime(''), '아직 없음');
});

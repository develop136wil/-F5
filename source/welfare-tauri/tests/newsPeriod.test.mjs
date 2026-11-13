import assert from 'node:assert/strict';
import test from 'node:test';
import { formatNewsPeriod } from '../src/utils/newsPeriod.js';

test('행사 시작일을 마감일로 오인시키지 않고 범위·단일일·미상을 구분한다', () => {
    assert.equal(formatNewsPeriod('2026-09-01', '2026-09-30'), '2026-09-01 ~ 2026-09-30');
    assert.equal(formatNewsPeriod('2026-09-01', '2026-09-01'), '일정 2026-09-01');
    assert.equal(formatNewsPeriod('2026-09-01', null), '일정 2026-09-01');
    assert.equal(formatNewsPeriod(null, '2026-09-30'), '일정 2026-09-30');
    assert.equal(formatNewsPeriod(null, null), '기간 미상');
});

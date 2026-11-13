import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { readPolicyReviewQueue } from '../src/utils/policyReviewQueue.js';

const codes = ['source_field_incomplete', 'structured_evidence_missing', 'source_schema_invalid',
    'source_conditions_incomplete', 'source_range_invalid', 'source_fact_changed',
    'source_evidence_mismatch', 'source_evidence_boundary_changed', 'source_presentation_mismatch',
    'source_evidence_missing'];

test('AI 검증 보류를 빈 대기 목록이나 완료로 바꾸지 않고 표시한다', () => {
    const items = codes.map((code, index) => ({id: `ai-${index}`, code, status: 'unresolved',
        title: '정책 검토', note: 'AI 응답의 원문 근거를 확인해주세요. 기존 내용은 유지됩니다.',
        source_url: 'https://example.invalid/policy', last_seen_at: '2026-09-14T00:00:00Z',
        fields: ['age_detail', 'contact'], raw_response: 'PRIVATE MODEL CONTENT',
        diagnostics: {quote: 'PRIVATE SOURCE CONTENT'}}));
    const before = structuredClone(items);
    const queue = readPolicyReviewQueue({items, total: items.length, truncated: false});
    assert.equal(queue.total, codes.length);
    assert.equal(queue.items.length, codes.length);
    assert.equal(queue.hasMore, false);
    for (const item of queue.items) {
        assert.match(item.note, /AI 응답/);
        assert.match(item.nextStep, /자동 반영은 보류/);
        assert.doesNotMatch(item.nextStep, /다시 점검|재시도|반복 실행/);
        assert.deepEqual(Object.keys(item), ['id', 'title', 'note', 'sourceUrl', 'lastSeenAt', 'nextStep']);
    }
    assert.doesNotMatch(JSON.stringify(queue), /PRIVATE|raw_response|diagnostics/);
    assert.deepEqual(items, before);
});

test('목록 문구는 원문과 AI 응답의 검토를 함께 안내한다', () => {
    const component = readFileSync(new URL('../src/components/PolicyReviewQueue.jsx', import.meta.url), 'utf8');
    assert.ok(component.includes('변경 제안 없이 확인이 필요한 정책 · 보류 사유 {query.data.total}건'));
    assert.ok(component.includes('원문과 AI 분석의 보류 사유를 확인해주세요.'));
    assert.ok(component.includes('저장된 기록만 읽습니다. AI를 호출하지 않습니다.'));
    assert.ok(component.includes('변경이 없다는 뜻은 아닙니다.'));
    assert.ok(component.includes('보류 기록을 불러오지 못했습니다. 확인할 정책이 없다는 뜻은 아닙니다.'));
    assert.ok(!component.includes('원문 확인 대기'));
});

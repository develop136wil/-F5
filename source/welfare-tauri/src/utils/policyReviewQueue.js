import { policyReviewNextStep } from './policyReviewOutcome.js';

export const POLICY_REVIEW_PAGE_SIZE = 50;

const text = (value, max) => typeof value === 'string' ? value.slice(0, max) : '';

export function policyReviewSourceUrl(value) {
    if (typeof value !== 'string' || value.length > 4096 || /[\s\u0000-\u001f\u007f]/.test(value)) return '';
    try {
        const url = new URL(value);
        return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : '';
    } catch { return ''; }
}

// Unknown or malformed responses must not look like a successful empty queue.
export function readPolicyReviewQueue(data) {
    if (!data || !Array.isArray(data.items) || data.items.length > POLICY_REVIEW_PAGE_SIZE ||
        !Number.isSafeInteger(data.total) || data.total < data.items.length || typeof data.truncated !== 'boolean') {
        throw new Error('보류 기록의 응답을 확인하지 못했습니다. 다시 불러와주세요.');
    }
    const ids = new Set();
    const items = data.items.map(item => {
        if (!item || typeof item.id !== 'string' || !item.id || ids.has(item.id) ||
            item.status !== 'unresolved' || typeof item.note !== 'string' || !item.note.trim()) {
            throw new Error('보류 기록의 응답을 확인하지 못했습니다. 다시 불러와주세요.');
        }
        ids.add(item.id);
        return {
            id: item.id, title: text(item.title, 120) || '제목을 확인할 정책',
            note: text(item.note, 500), sourceUrl: policyReviewSourceUrl(item.source_url),
            lastSeenAt: text(item.last_seen_at, 40), nextStep: policyReviewNextStep(item.code),
        };
    });
    return { items, total: data.total, hasMore: data.truncated };
}

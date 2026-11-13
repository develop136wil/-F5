import { policyReviewNextStep } from './policyReviewOutcome.js';

// Old/unknown responses remain readable; omission never grants write permission.
export const policyRecoveryRequired = policy => Array.isArray(policy?.apply_history) && policy.apply_history.some(record => record.needs_verification);
export const policyCanApply = policy => policy?.apply_ready === true && policy.review_state === 'proposal' && !policyRecoveryRequired(policy);

export function policyStateLabel(policy) {
    if (policyRecoveryRequired(policy)) return '반영 확인 필요';
    if (policy?.review_state === 'completed') return '반영 완료';
    if (Array.isArray(policy?.review_notes) && policy.review_notes.some(note=>note?.code==='source_too_long')) return '상세 출처 연결 필요';
    if (policy?.review_state === 'source_review') return '원문 확인 필요';
    if (!policyCanApply(policy)) return '재점검 필요';
    return '';
}

export function policyCardReviewNotes(policy) {
    return (Array.isArray(policy?.review_notes) ? policy.review_notes : [])
        .filter(note => note && typeof note.note === 'string' && note.note.trim())
        .map(note => note.code === 'source_too_long'
            ? {note:'원문이 길어 분석을 완료하지 못했습니다. 해당 정책의 상세 페이지를 출처로 연결한 뒤 다시 점검해주세요.',nextStep:''}
            : { note: note.note.slice(0, 1000), nextStep: policyReviewNextStep(note.code) })
        .filter((note,index,notes)=>notes.findIndex(other=>other.note===note.note&&other.nextStep===note.nextStep)===index);
}

export function readPolicyChanges(data) {
    if (!data || !Array.isArray(data.items) || data.items.some(item => !item ||
        !Number.isSafeInteger(item.id) || !Array.isArray(item.fields))) {
        throw new Error('변경 목록의 응답을 확인하지 못했습니다. 다시 불러와주세요.');
    }
    return data;
}

export function policyWatchOutcome(result) {
    const { checked, changed, failed, status } = result || {};
    if (![checked, changed, failed].every(n => Number.isSafeInteger(n) && n >= 0) ||
        failed > checked || changed > checked - failed || !['completed', 'partial', 'error'].includes(status) ||
        (failed > 0 && status === 'completed') || (failed === 0 && status !== 'completed')) {
        throw new Error('점검 결과를 확인하지 못했습니다. 변경이 없다는 뜻은 아닙니다.');
    }
    if (failed > 0) return {
        level: status === 'error' ? 'error' : 'warning',
        message: `점검 ${status === 'error' ? '실패' : '일부 완료'} — 확인 성공 ${checked - failed}건 · 변경 ${changed}건 · 읽기 실패 ${failed}건. 실패한 페이지는 변경 여부를 확인하지 못했습니다.`,
    };
    return { level: 'success', message: `점검 완료 — ${checked}건 중 변경 ${changed}건` };
}

export const policyWriteNeedsRecovery = item => ['UPDATE_UNCERTAIN', 'LOCAL_SAVE_FAILED', 'APPLY_RESULT_UNCERTAIN'].includes(item?.error) || item?.reason === 'NOT_ATTEMPTED';

export function policyNeedsReview(status) {
    return !status?.running && (
        (Number.isFinite(status?.review_count) && status.review_count > 0) ||
        (Number.isFinite(status?.ai_failed) && status.ai_failed > 0)
    );
}

export function policyReviewNotes(status) {
    if (!Array.isArray(status?.review_notes)) return [];
    return status.review_notes.filter(note => note && typeof note.note === 'string')
        .slice(0, 20).map(note => ({
            title: typeof note.title === 'string' ? note.title.slice(0, 120) : '정책 확인',
            note: note.note.slice(0, 500),
            nextStep: policyReviewNextStep(note.code),
        }));
}

// Full-check mirrors the underlying check's review notes. Restore one banner
// only when the visible reasons and counts really match; keep distinct results.
export function policyReviewIsMirrored(status, fullStatus) {
    if (!policyNeedsReview(status) || !policyNeedsReview(fullStatus)) return false;
    const notes = policyReviewNotes(status);
    return notes.length > 0 && status.review_count === fullStatus.review_count &&
        (status.ai_failed || 0) === (fullStatus.ai_failed || 0) &&
        JSON.stringify(notes) === JSON.stringify(policyReviewNotes(fullStatus));
}

export function policyReviewNextStep(code) {
    if (code === 'source_criteria_review_required') {
        return '먼저 원문에서 적용 대상·기준일을 확인하고, 모호하면 담당 기관에 문의하세요. 같은 원문을 반복 점검해도 해결되지 않습니다. 출처가 명확하게 수정된 뒤 다시 점검하세요.';
    }
    if (code === 'source_too_long') {
        return '여러 정책이 모인 목록 대신 해당 정책의 상세 출처를 연결한 뒤 다시 점검하세요.';
    }
    if (code === 'ai_input_review_required') {
        return '민감정보가 포함됐는지 먼저 확인하세요. 전송 가능한 공개 출처인지 확인하기 전에는 반복 실행하지 마세요.';
    }
    return '출처와 보류 사유를 먼저 확인하세요. 자동 반영은 보류된 상태이며, 필요한 경우 Notion에서 직접 내용을 검토하세요.';
}

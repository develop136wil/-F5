const outcomes = {match: '대상 조건에 맞음', review: '검토 필요', excluded: '조건에서 제외', legacy: '기존 방식 분석'};
const states = {queued: '실행 대기', running: '분석 중', waiting: '재시도 대기', failed: '분석 미완료',
    analysis_unconfigured: 'AI 분석 준비 대기', semantic_candidate: '추가 분석 대기',
    semantic_review: '의미 유사도가 낮아 검토 필요', excluded: '제외 단어에 해당 · AI 분석 안 함'};
const unavailable = {
    relevance_unconfirmed: '관련 소식인지 확인되지 않아 후보로 가져올 수 없습니다. 원문을 확인해주세요.',
    excluded: '현재 조건에서 제외된 자료입니다. 원문과 분석 기록은 계속 확인할 수 있습니다.',
    unsupported_analysis: '이전 방식의 분석 결과입니다. 원문과 분석 기록을 확인해주세요.',
    import_text_too_long: '본문이 길어 새 소식 후보로 가져오기를 보류했습니다. 분석 기록과 원문은 유지됩니다.',
    invalid_saved_result: '저장된 분석의 근거를 확인하지 못해 가져오기를 보류했습니다. 원문과 분석 기록을 확인해주세요.',
};
const reviewLabels = {
    ai_input_review_required: '개인정보·비밀정보 확인 필요 · AI 전송 보류',
    ai_input_size_review_required: '입력 분량 확인 필요 · 분석 보류',
    ai_source_review_required: '원문 형식 확인 필요 · 분석 보류',
    ai_response_review_required: 'AI 응답 확인 필요 · 분석 보류',
};

export function newsResultPresentation(item) {
    const complete = item?.state === 'complete';
    const source = item?.source_review?.status;
    const needsSourceReview = source === 'partial' || source === 'empty' || source === 'unknown';
    let statusLabel = complete ? (outcomes[item.target_status] || '결과 확인 필요') : (states[item?.state] || '상태 확인 필요');
    if (complete && item.target_status === 'match' && needsSourceReview) {
        statusLabel = source === 'empty' ? '본문을 확보하지 못함 · 원문 확인 필요' : source === 'unknown'
            ? '확보한 내용 기준 일치 · 원문 확보 범위 확인 필요' : '확보한 내용 기준 일치 · 원문 확인 필요';
    } else if (complete && source === 'empty' && item.target_status === 'review') {
        statusLabel = '본문을 확보하지 못함 · 검토 필요';
    }
    if (!complete && item?.state === 'failed' && reviewLabels[item?.review_reason]) {
        statusLabel = reviewLabels[item.review_reason];
    }
    if (Array.isArray(item?.source_review?.methods) && item.source_review.methods.includes('local_ocr')) {
        statusLabel += ' · 이미지 글자 활용';
    }
    const eligibility = item?.import_eligibility;
    // Absence/unknown shape cannot inherit authority from an AI match flag.
    // The current server derives this metadata for historical saved results too.
    const canImport = complete && !!item.job_id && ['match', 'review'].includes(item.target_status)
        && eligibility?.allowed === true && eligibility?.reason === 'confirmation_required';
    let importNote = '';
    if (complete && item.job_id && !canImport) {
        importNote = unavailable[eligibility?.reason] || '가져오기 가능 여부를 확인하지 못했습니다. 새로고침 후 다시 확인해주세요.';
    }
    return {statusLabel, needsSourceReview, canImport, importNote};
}

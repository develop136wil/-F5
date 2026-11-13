export const newsDetailLabels = {
    category:'분류', target:'대상', applicant_detail:'신청하는 사람·기관',
    beneficiary_detail:'실제로 지원받는 대상', applicant_evidence:'신청 대상의 원문 근거',
    support_detail:'지원 내용', eligibility_detail:'신청 조건', contact:'문의', deadline:'기한',
};

export function fitsDetailLimit(text) {
    // Python's len counts Unicode code points, not UTF-16 surrogate units.
    if (typeof text !== 'string' || text.length > 60000) return false;
    let count = 0;
    for (const _point of text) if (++count > 30000) return false;
    return true;
}

const presentationKinds = new Set(['source_excerpt', 'structured', 'legacy']);
function fitsSourceNote(text) {
    return typeof text === 'string' && text.length <= 2000 && [...text].length <= 1000;
}

function validPresentationMetadata(value) {
    return (value.presentation_kind === undefined || presentationKinds.has(value.presentation_kind))
        && (value.source_note === undefined || fitsSourceNote(value.source_note));
}

// An explicit empty projection must not fall back to ai_data's raw source text.
export function storedNewsPresentation(item) {
    if (!item || item.news_presentation === undefined) return null;
    const value = item.news_presentation;
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || !fitsDetailLimit(value.support_detail) || !validPresentationMetadata(value)) {
        return {support_detail:'', source_note:'지원 내용을 확인하지 못했습니다. 원문을 확인해주세요.'};
    }
    return {support_detail:value.support_detail, presentation_kind:value.presentation_kind,
        source_note:value.source_note || ''};
}

export function detailEntries(details) {
    return Object.entries(newsDetailLabels).filter(([key]) => typeof details?.[key] === 'string'
        && details[key].trim() && fitsDetailLimit(details[key]))
        .map(([key, label]) => ({key, label, text:details[key]}));
}

export function detailOmissions(omissions) {
    return Object.entries(newsDetailLabels).filter(([key]) => Number.isSafeInteger(omissions?.[key]?.length)
        && omissions[key].length > 0).map(([key, label]) => ({key, label, length:omissions[key].length,
            reason:omissions[key].reason, available:omissions[key].available === true}));
}

export function checkedDetailResponse(value, recordId) {
    if (!value || value.record_id !== recordId || !value.details || typeof value.details !== 'object'
        || Array.isArray(value.details) || value.ai_called !== false || !validPresentationMetadata(value)
        || Object.entries(value.details).some(([key, text]) => !Object.hasOwn(newsDetailLabels, key)
            || !fitsDetailLimit(text))) {
        throw Error('분석 내용을 확인하지 못했습니다. 다시 불러오거나 원문을 확인해주세요.');
    }
    return value;
}

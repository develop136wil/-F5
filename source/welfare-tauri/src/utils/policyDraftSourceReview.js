export const draftSourceReviewItems = Object.freeze([
    {key: 'review', label: '원문과 비교하여 초안을 확인했습니다.', detail: '확인되지 않은 내용은 수정하거나 비워두었습니다.'},
]);

export function emptyDraftSourceChecks() {
    return Object.fromEntries(draftSourceReviewItems.map(({key}) => [key, false]));
}

export function draftIncomeReviewRequired(value) {
    return Array.isArray(value?.reasons)
        && value.reasons.includes('draft_income_review_required');
}

const coverageLabels=Object.freeze({html_byte_limit:'원문 크기 제한',page_char_limit:'본문 길이 제한',
    decode_error:'문자 해석 실패',rendered_capture_partial:'화면 일부만 읽음',
    attachment_review:'첨부 확인 필요',fetch_failed:'원문 접근 실패',no_text:'본문 없음'});
export function draftSourceCoverageNotice(value){
    const reasons=Array.isArray(value?.reasons)?value.reasons:[];
    const labels=[...new Set(reasons.filter(reason=>typeof reason==='string'&&Object.hasOwn(coverageLabels,reason)))].map(reason=>coverageLabels[reason]);
    return labels.length?`원문 일부를 확인하지 못했습니다: ${labels.join(' · ')}. 빠진 조건은 원문에서 확인하고 수정하거나 비워두세요.`:'';
}

export function draftAgeRangeError(fields){
    const ages=[fields?.start_age_months,fields?.end_age_months];
    const entered=value=>value!==null&&value!==undefined&&value!=='';
    if(ages.some(value=>entered(value)&&(!Number.isInteger(value)||value<0||value>1800)))
        return '월령은 0~1800 사이의 정수로 입력하거나 비워두세요.';
    return ages.every(entered)&&ages[0]>ages[1]?'시작 월령은 종료 월령보다 클 수 없습니다.':'';
}

export function updateDraftSourceCheck(previous, key, value) {
    return Object.fromEntries(draftSourceReviewItems.map(item => [item.key,
        (item.key === key ? value : previous?.[item.key]) === true]));
}

export function draftSourceConfirmed(checks) {
    return draftSourceReviewItems.every(({key}) => checks?.[key] === true);
}

const fieldLabels = Object.freeze({
    title: '사업명', category: '분류', target: '대상', residence: '거주지',
    target_characteristics: '대상 특성', life_cycle: '생애 주기',
    start_age_months: '시작 월령', end_age_months: '종료 월령', age_detail: '나이 조건',
    target_income: '소득 조건', eligibility_detail: '신청 조건', support_detail: '지원 내용·비용',
    contact: '연락처', deadline: '접수 마감', event_date: '시작일', event_end_date: '종료일', related_urls: '관련 주소',
});

export function draftSourceReview(value) {
    // Display only bounded text and known field labels, never raw diagnostic/model objects.
    const source = typeof value?.source_text === 'string' ? value.source_text : '';
    let fits = source.length <= 60000;
    if (fits) {
        let count = 0;
        for (const _point of source) if (++count > 30000) {fits = false; break;}
    }
    const unverified = value?.unverified_fields;
    const keys = Array.isArray(unverified) ? unverified
        : unverified && typeof unverified === 'object' ? Object.keys(unverified) : [];
    return {
        sourceText: fits ? source : '',
        sourceUnavailable: !fits || !source.trim(),
        fields: [...new Set(keys.slice(0, 100)
            .filter(key => typeof key === 'string' && Object.hasOwn(fieldLabels, key)))]
            .map(key => fieldLabels[key]),
    };
}

export function requireDraftSourceConfirmation(confirmed) {
    if (confirmed !== true) {
        const error = new Error('비용·대상·접수 및 이용 기간·연락처를 원문과 대조한 뒤 확인해주세요.');
        error.code = 'POLICY_SOURCE_REVIEW_REQUIRED';
        throw error;
    }
}

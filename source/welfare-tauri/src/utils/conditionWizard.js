import {validateAges} from './newsProfileForm.js';

export function validateConditionDraft(draft) {
    const region = typeof draft.region === 'string' ? draft.region.trim() : '';
    if (!region || region.length > 100 || /[\u0000-\u001f\u007f]/.test(region))
        throw new Error('지역을 1~100자의 한 줄로 입력해주세요.');
    const ages = validateAges(draft.min_age_months, draft.max_age_months);
    for(const [key,label] of [['keywords','찾을 단어'],['excluded_keywords','제외할 단어']]) {
        if(draft[key] != null && (!Array.isArray(draft[key]) || draft[key].length > 500 || draft[key].some(word=>typeof word!=='string')))
            throw new Error(`${label}는 500개까지 입력할 수 있습니다.`);
    }
    if(draft.ai_prompt != null && (typeof draft.ai_prompt!=='string' || draft.ai_prompt.length>10000))
        throw new Error('AI 요청은 10,000자까지 입력할 수 있습니다.');
    return {region, min_age_months:ages.start_age_months, max_age_months:ages.end_age_months};
}

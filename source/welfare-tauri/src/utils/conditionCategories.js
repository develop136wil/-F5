export const conditionCategories = Object.freeze({
    life_cycles:Object.freeze(['임신·출산','영유아','아동','청소년','청년','중장년','노년']),
    characteristics:Object.freeze(['장애인','한부모·조손','다자녀','1인 가구','다문화','탈북민','보훈','여성']),
});

// Preserve saved legacy/custom labels visibly, without silently translating them.
export function editableCategoryOptions(key, selected = []) {
    return [...new Set([...(conditionCategories[key] || []), ...selected])];
}

export function officialConditionSupport(value, taxonomy) {
    if (!taxonomy) return {available:false, supported:false, needsReview:false};
    const aliases={'다문화':'다문화·탈북민','탈북민':'다문화·탈북민','보훈':'보훈대상자','노인':'노년'};
    const groups=[['life_cycles','life_cycles'],['characteristics','household_situations'],['interest_topics','interest_topics']];
    let supported=false, needsReview=false;
    for(const [key,official] of groups) for(const tag of value[key] || []) {
        if(tag==='일반') continue;
        const mapped=aliases[tag] || tag;
        const found=(taxonomy[official] || []).includes(mapped);
        supported ||= found;
        needsReview ||= !found || mapped!==tag;
    }
    return {available:true,supported,needsReview};
}

// Fresh conditions never inherit infant age limits, keywords or AI instructions.
export function newCollectionCondition(preset, sites = []) {
    return {
        name: preset?.name || '직접 만든 수집 조건', region: '전국',
        min_age_months: null, max_age_months: null,
        life_cycles: [...(preset?.life_cycles || [])], characteristics: [...(preset?.characteristics || [])],
        interest_topics: [...(preset?.interest_topics || [])],
        keywords: [...(preset?.keywords || [])], excluded_keywords: [],
        site_ids: sites.map(site => String(site.id)), sources: ['sites'],
        ai_prompt: '', semantic_text: preset?.semantic_text || '복지 지원과 생활에 도움이 되는 소식',
        relevance_enabled: true,
        publication_period: '30', publication_start_date: '', publication_end_date: '',
    };
}

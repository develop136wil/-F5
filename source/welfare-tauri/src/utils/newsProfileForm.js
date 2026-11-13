export const profileFields = ['name', 'region', 'min_age_months', 'max_age_months',
    'life_cycles', 'characteristics', 'interest_topics', 'keywords', 'excluded_keywords', 'site_ids',
    'sources', 'ai_prompt', 'semantic_text', 'relevance_enabled',
    'publication_period', 'publication_start_date', 'publication_end_date'];

export function editableSettings(profile) {
    return Object.fromEntries(profileFields.map(key => [key, structuredClone(profile[key])]));
}

export function applyProfilePreset(draft, preset) {
    if (!draft || !preset) return draft;
    return {
        ...draft,
        name: preset.name,
        min_age_months: '',
        max_age_months: '',
        life_cycles: (preset.life_cycles || []).join(', '),
        characteristics: (preset.characteristics || []).join(', '),
        interest_topics: (preset.interest_topics || []).join(', '),
        keywords: (preset.keywords || []).join(', '),
        semantic_text: preset.semantic_text || '',
    };
}

export function parseTags(value) {
    return [...new Set(value.split(/[,\n]/).map(s => s.trim()).filter(Boolean))];
}

// This compares settings only; it does not judge the meaning of AI instructions.
export function targetChanged(profile, draft) {
    if (!profile || !draft) return false;
    const tags = value => [...new Set(Array.isArray(value) ? value : parseTags(value || ''))].sort();
    const age = value => value === '' || value == null ? null : Number(value);
    return profile.region.trim() !== draft.region.trim()
        || ['min_age_months', 'max_age_months'].some(key => age(profile[key]) !== age(draft[key]))
        || ['life_cycles', 'characteristics', 'interest_topics', 'keywords', 'excluded_keywords'].some(key =>
            JSON.stringify(tags(profile[key])) !== JSON.stringify(tags(draft[key])));
}

export function toggleTag(value, tag) {
    const tags = parseTags(value || '');
    const next = tags.includes(tag) ? tags.filter(item => item !== tag) : [...tags, tag];
    return next.join(', ');
}

export function normalizeCollectionSources(value) {
    if (!Array.isArray(value)) return [];
    const selected = new Set(value);
    return ['sites', 'bokjiro'].filter(source => selected.has(source));
}

export function toggleCollectionSource(value, source, checked) {
    if (!['sites', 'bokjiro'].includes(source)) throw new Error('알 수 없는 수집 위치입니다.');
    const selected = new Set(normalizeCollectionSources(value));
    if (checked) selected.add(source); else selected.delete(source);
    return ['sites', 'bokjiro'].filter(item => selected.has(item));
}

export function parseMonths(value) {
    if (value === '') return null;
    if (!/^\d+$/.test(String(value)) || Number(value) > 1800)
        throw new Error('연령은 0~1800개월의 정수로 입력해주세요. 제한이 없으면 비워두세요.');
    return Number(value);
}

export function validateAges(min, max) {
    const low = parseMonths(min), high = parseMonths(max);
    if (low !== null && high !== null && low > high) throw new Error('시작 연령이 종료 연령보다 큽니다.');
    return { start_age_months: low, end_age_months: high };
}

export function ageHint(value) {
    if (value === '') return '나이 제한 없음';
    try {
        const months = parseMonths(value);
        const years = Math.floor(months / 12), rest = months % 12;
        return `만 ${years}세${rest ? ` ${rest}개월` : ''} (${months}개월)`;
    } catch { return '0 이상의 개월 수를 입력해주세요'; }
}

export function describeSearchMatch(item, semanticMode = false) {
    if (!semanticMode) return null;
    const sources = new Set(Array.isArray(item?.match_sources) ? item.match_sources : []);
    if (sources.has('keyword') && sources.has('semantic')) {
        return item?.keyword_match === 'partial_terms'
            ? { label: '핵심 단어·비슷한 뜻', tone: 'strong' }
            : { label: '검색어·의미 모두 일치', tone: 'strong' };
    }
    if (sources.has('keyword')) {
        return {
            label: item?.keyword_match === 'partial_terms' ? '핵심 단어 일부 일치' : '검색어 일치',
            tone: 'keyword',
        };
    }
    if (sources.has('semantic')) return { label: '비슷한 뜻', tone: 'semantic' };
    return null;
}

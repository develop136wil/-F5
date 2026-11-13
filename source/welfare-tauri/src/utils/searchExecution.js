export function resolveSearchExecution({ semanticMode, query, category }) {
    const normalizedQuery = String(query || '').trim();
    const normalizedCategory = String(category || '').trim();
    const useSemanticSearch = Boolean(semanticMode && normalizedQuery.length >= 2);

    return {
        useSemanticSearch,
        enabled: useSemanticSearch || !semanticMode || normalizedCategory.length > 0,
    };
}

// A failed or pending query must never look like a verified empty result.
export function queryCountStatus(query, count, label) {
    const known = Number.isInteger(count) && count >= 0;
    if (query.isError) return known ? `${label} ${count}건 · 이전 조회값 (갱신 실패)` : '확인하지 못했습니다';
    if (!known) return '확인 중…';
    return `${label} ${count}건${query.isFetching ? ' · 갱신 중' : ''}`;
}

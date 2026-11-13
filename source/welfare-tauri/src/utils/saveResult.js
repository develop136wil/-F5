export const formatSaveResult = result => {
    if (!result) return '';
    const saved = Math.max(0, Number(result.saved) || 0);
    const skipped = Math.max(0, Number(result.skipped) || 0);
    return `${saved}건 저장/갱신${skipped ? ` · ${skipped}건 중복 제외` : ''}`;
};

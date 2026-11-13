export function publicationSettings(value = {}) {
    const period = value.publication_period ?? '30';
    if (!['7', '30', '90', 'custom'].includes(period)) throw Error('수집 기간을 선택해주세요.');
    const start = value.publication_start_date || '', end = value.publication_end_date || '';
    const validDate = text => /^\d{4}-\d{2}-\d{2}$/.test(text) && Number(text.slice(0,4)) >= 1 && !Number.isNaN(Date.parse(text + 'T00:00:00Z'))
        && new Date(text + 'T00:00:00Z').toISOString().slice(0, 10) === text;
    if (period === 'custom' && (!validDate(start) || !validDate(end) || start > end))
        throw Error('게시일의 시작일과 종료일을 올바르게 입력해주세요.');
    return {publication_period: period, publication_start_date: start, publication_end_date: end};
}

export function publicationLabel(value = {}) {
    if (value.publication_period === 'custom') return (value.publication_start_date || '?') + ' ~ ' + (value.publication_end_date || '?');
    return '최근 ' + (['7', '30', '90'].includes(value.publication_period) ? value.publication_period : '30') + '일';
}

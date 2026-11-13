// The API returns a display period, not necessarily an application deadline.
export function formatNewsPeriod(start, end) {
    if (start && end && start !== end) return `${start} ~ ${end}`;
    return start || end ? `일정 ${start || end}` : '기간 미상';
}

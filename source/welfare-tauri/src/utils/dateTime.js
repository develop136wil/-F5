export const formatStoredUtcTime = value => {
    if (!value) return '아직 없음';
    const raw = String(value).trim();
    const iso = raw.includes('T') ? raw : raw.replace(' ', 'T');
    // SQLite CURRENT_TIMESTAMP는 UTC지만 시간대 표식 없이 저장된다.
    const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso);
    const date = new Date(hasTimezone ? iso : `${iso}Z`);
    if (Number.isNaN(date.getTime())) return raw.replace('T', ' ').slice(0, 16);
    return new Intl.DateTimeFormat('ko-KR', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
};

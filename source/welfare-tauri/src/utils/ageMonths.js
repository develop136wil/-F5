export const DEFAULT_MAX_AGE_MONTHS = 240;

const integerOrZero = (value) => {
    if (value === '' || value === null || value === undefined) return 0;
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
};

export const splitAgeMonths = (value, maxMonths = DEFAULT_MAX_AGE_MONTHS) => {
    const total = Math.min(integerOrZero(value), maxMonths);
    return { years: Math.floor(total / 12), months: total % 12, total };
};

export const combineAgeParts = (years, months, maxMonths = DEFAULT_MAX_AGE_MONTHS) => {
    const total = integerOrZero(years) * 12 + Math.min(integerOrZero(months), 11);
    return Math.min(total, maxMonths);
};

export const formatAgeMonths = (value) => {
    const { years, months, total } = splitAgeMonths(value, Number.MAX_SAFE_INTEGER);
    const age = `만 ${years}세${months ? ` ${months}개월` : ''}`;
    return `${age} · 저장값 ${total}개월`;
};

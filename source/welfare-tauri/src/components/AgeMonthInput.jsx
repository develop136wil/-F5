import React from 'react';
import { combineAgeParts, DEFAULT_MAX_AGE_MONTHS, formatAgeMonths, splitAgeMonths } from '../utils/ageMonths';

const fieldClass = 'w-24 px-3 py-2 border border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 dark:text-gray-100';

export default function AgeMonthInput({ value, onChange, idPrefix = 'age', maxMonths = DEFAULT_MAX_AGE_MONTHS, disabled = false, suffix = '까지', accessibleLabel = '대상 나이' }) {
    const { years, months } = splitAgeMonths(value, maxMonths);
    const maxYears = Math.floor(maxMonths / 12);

    return (
        <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
                <label htmlFor={`${idPrefix}-years`} className="sr-only">{accessibleLabel} 만 나이</label>
                <input
                    id={`${idPrefix}-years`}
                    type="number"
                    inputMode="numeric"
                    min={0}
                    max={maxYears}
                    value={years}
                    disabled={disabled}
                    onChange={(event) => onChange(combineAgeParts(event.target.value, months, maxMonths))}
                    className={fieldClass}
                />
                <span className="text-sm text-gray-600 dark:text-gray-300">세</span>
                <label htmlFor={`${idPrefix}-months`} className="sr-only">{accessibleLabel} 추가 개월</label>
                <input
                    id={`${idPrefix}-months`}
                    type="number"
                    inputMode="numeric"
                    min={0}
                    max={11}
                    value={months}
                    disabled={disabled}
                    onChange={(event) => onChange(combineAgeParts(years, event.target.value, maxMonths))}
                    className={fieldClass}
                />
                <span className="text-sm text-gray-600 dark:text-gray-300">개월{suffix}</span>
            </div>
            <p className="text-xs text-gray-500">{formatAgeMonths(value)}로 자동 계산됩니다.</p>
        </div>
    );
}

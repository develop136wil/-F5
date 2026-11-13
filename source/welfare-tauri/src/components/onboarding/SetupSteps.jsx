import React from 'react';
import {Check} from 'lucide-react';

// One navigation pattern for every setup journey; completion and selection are independent.
export default function SetupSteps({items, done, activeKey, disabled, onSelect, label}) {
    return <nav aria-label={label} className={`mt-5 grid gap-2 ${items.length === 5 ? 'grid-cols-2 sm:grid-cols-5' : items.length === 2 ? 'grid-cols-2' : 'grid-cols-3'}`}>
        {items.map((item, index) => <button key={item.key} type="button" disabled={disabled}
            aria-label={`${index + 1}. ${item.label}${done[item.key] ? ' · 완료' : ' · 설정 필요'}${activeKey === item.key ? ' · 현재 단계' : ''}`}
            aria-current={activeKey === item.key ? 'step' : undefined} onClick={() => onSelect(item.key)}
            className={`min-h-12 min-w-0 rounded-xl border px-2 py-3 text-left disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${activeKey === item.key ? 'border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/40' : 'border-transparent hover:bg-gray-50 dark:hover:bg-gray-900/30'}`}>
            <span aria-hidden="true" className="flex flex-col items-start gap-2 sm:flex-row sm:items-center">
                <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold ${done[item.key] ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : activeKey === item.key ? 'border-blue-600 bg-blue-600 text-white' : 'border-gray-300 text-gray-500 dark:border-gray-600 dark:text-gray-400'}`}>
                    {done[item.key] ? <Check className="h-4 w-4"/> : index + 1}
                </span>
                <span className={`text-sm leading-snug ${activeKey === item.key ? 'font-semibold text-blue-800 dark:text-blue-200' : 'font-medium text-gray-600 dark:text-gray-300'}`}>{item.label}</span>
            </span>
        </button>)}
    </nav>;
}

import React from 'react';

export default function SetupTemplateChooser({done,disabled,onSelect}) {
    return <div role="group" aria-label="템플릿 선택" className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {[{key:'policy',title:'정책 최신화',note:'정책표 복제 · 주소 등록'},{key:'news',title:'새 소식 탐색',note:'보관함 복제 · 주소 등록'}].map(item=><button key={item.key} type="button" disabled={disabled} onClick={()=>onSelect(item.key)} className="flex min-h-36 flex-col items-start gap-3 rounded-2xl border border-indigo-200 bg-indigo-50/40 p-5 text-left hover:border-indigo-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-600 dark:border-indigo-800 dark:bg-indigo-950/20 disabled:opacity-50">
            <span className="text-lg font-semibold">{item.title}</span>
            <span className="text-sm text-gray-600 dark:text-gray-300">{item.note}</span>
            <span className={done[item.key]?'text-sm font-semibold text-emerald-700 dark:text-emerald-300':'text-sm text-indigo-700 dark:text-indigo-300'}>{done[item.key]?'✓ 설정 완료 · 다시 보기':'설정하기 →'}</span>
        </button>)}
    </div>;
}

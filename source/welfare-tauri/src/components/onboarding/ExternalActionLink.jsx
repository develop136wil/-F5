import React from 'react';
import { ExternalLink } from 'lucide-react';

const TONES = {
    blue: {
        border: 'border-blue-200 dark:border-blue-800',
        title: 'text-blue-700 dark:text-blue-300',
    },
    emerald: {
        border: 'border-teal-200 dark:border-teal-800',
        title: 'text-teal-700 dark:text-teal-300',
    },
    gray: {
        border: 'border-gray-200 dark:border-gray-700',
        title: 'text-gray-800 dark:text-gray-100',
    },
};

/**
 * 외부 설정 링크 공통 표현.
 * 현재 단계에서 누를 외부 행동만 버튼으로 표현한다.
 * 선택 설명은 링크와 분리해 반복되는 배지·안내 행을 만들지 않는다.
 */
const ExternalActionLink = ({
    href,
    title,
    description,
    after,
    tone = 'gray',
    official = true,
    className = '',
}) => {
    const palette = TONES[tone] || TONES.gray;
    return (
        <div className={`min-w-0 ${className}`}>
        <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`${title} — 새 창으로 열림`}
            title={official ? '공식 페이지 · 새 창에서 열립니다' : '새 창에서 열립니다'}
            className={`inline-flex min-h-10 max-w-full items-center gap-2 rounded-lg border bg-white px-3 py-2 text-left text-sm font-semibold transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:bg-gray-900 dark:hover:bg-gray-800 ${palette.border} ${palette.title}`}
        >
            <span className="min-w-0 break-words">{title}</span>
            <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        </a>
        {description && <p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">{description}</p>}
        {after && <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">{after}</p>}
        </div>
    );
};

export default ExternalActionLink;

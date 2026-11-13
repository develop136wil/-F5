import React from 'react';
import { Link } from 'react-router-dom';
import { useAiStatus } from '../hooks/useAiStatus';
import { aiUsagePresentation } from '../utils/aiUsagePresentation';
import { Tooltip } from './ui/Tooltip';

export function PaidApiHelp({ compact = false }) {
    const note = '유료 프로젝트는 사용량에 따라 비용이 발생할 수 있습니다. 앱에서 결제 설정을 변경하지 않습니다.';
    const link = <a href="https://ai.google.dev/gemini-api/docs/billing?hl=ko" target="_blank" rel="noopener noreferrer" className="text-blue-600 underline underline-offset-2">Google 유료 이용 안내 ↗</a>;
    return <p className={`${compact ? '' : 'mt-3'} text-xs leading-5 text-gray-500 dark:text-gray-400`}>
        {compact ? <Tooltip content={note}>{link}</Tooltip> : <>{link}<span className="ml-2">{note}</span></>}
    </p>;
}

export default function AiUsageStatus() {
    const query = useAiStatus();
    const state = aiUsagePresentation(query);
    // Ordinary work progress belongs to the sidebar task gauge. Only exceptional
    // AI waits/errors need a separate explanation; the local count is not quota.
    if (!query.isError && state.tone !== 'warning') return null;
    return <section aria-label="AI 사용 상태" className={`mb-4 rounded-xl border px-4 py-3 text-sm ${state.tone === 'warning' ? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200' : 'border-gray-100 bg-white text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300'}`}>
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <p role="status" className="font-medium">{state.title}</p>
        </div>
        {state.note && <p className="mt-1 leading-6">{state.note}</p>}
        <details className="mt-1">
            <summary className="cursor-pointer text-xs underline underline-offset-2">사용량 기준·도움말</summary>
            <div className="mt-2 space-y-1 text-sm leading-6">
                <p>대기 안내는 Google의 남은 무료량을 나타내지 않습니다.</p>
                <p>묶음 임베딩은 텍스트 한 건을 요청 한 회로 보수적으로 집계합니다. 실제 통신 횟수·Google 사용량과 다를 수 있습니다.</p>
                <p>집계 날짜: {query.data?.usage_date || '확인 전'} · {query.data?.usage_timezone === 'UTC' ? 'UTC 기준' : '태평양 시간 기준'}. 모델별 실제 한도는 Google에서 확인하세요.</p>
                <a href="https://ai.google.dev/gemini-api/docs/rate-limits?hl=ko" target="_blank" rel="noopener noreferrer" className="mr-4 inline-block py-1 text-blue-600 underline">Google 한도 확인 방법 ↗</a>
                <Link to="/settings?tab=connections" className="inline-block py-1 text-blue-600 underline">연결 설정·선택적 유료 API 안내</Link>
            </div>
        </details>
    </section>;
}

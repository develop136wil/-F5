import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { formatStoredUtcTime } from '../utils/dateTime';
import { POLICY_REVIEW_PAGE_SIZE, readPolicyReviewQueue } from '../utils/policyReviewQueue';

export default function PolicyReviewQueue({ dbKeys, excludePolicyIds = [] }) {
    const [offset, setOffset] = useState(0);
    const [expanded, setExpanded] = useState(false);
    const query = useQuery({
        queryKey: ['policyReviews', dbKeys, excludePolicyIds, offset],
        queryFn: async ({ signal }) => readPolicyReviewQueue(await welfareApi.getPolicyReviews(dbKeys, offset, signal, excludePolicyIds)),
        enabled: dbKeys.length > 0,
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnMount: 'always',
    });
    const scopeKey = JSON.stringify([dbKeys, excludePolicyIds]);
    useEffect(() => setOffset(0), [scopeKey]);
    if (!dbKeys.length || (!query.isError && query.data?.total === 0)) return null;
    const button = 'min-h-11 rounded-lg border border-amber-200 bg-white px-3 py-2 text-sm text-amber-900 disabled:opacity-50 dark:border-amber-800 dark:bg-gray-900 dark:text-amber-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500';
    return <section aria-label="저장된 정책 보류 기록" aria-busy={query.isFetching} className="rounded-xl border border-amber-200 bg-amber-50/60 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
        {query.isError && <p role="alert" className="mb-2">보류 기록을 불러오지 못했습니다. 확인할 정책이 없다는 뜻은 아닙니다.{query.data && ' 아래는 마지막으로 불러온 기록입니다.'}</p>}
        {query.isPending && !query.isError && <p role="status">보류 기록 불러오는 중…</p>}
        {query.data && <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
            <summary className="min-h-11 cursor-pointer py-2 font-semibold">변경 제안 없이 확인이 필요한 정책 · 보류 사유 {query.data.total}건</summary>
            <p className="mt-2 leading-relaxed">변경 제안이 있는 정책의 확인 사유는 해당 카드에 함께 표시합니다. 아래는 제안 없이 점검이 보류된 기록이며, 같은 정책의 여러 사유가 포함될 수 있습니다. 변경이 없다는 뜻은 아닙니다.</p>
            <p className="mt-2 text-gray-600 dark:text-gray-300">원문과 AI 분석의 보류 사유를 확인해주세요. 해당 보류가 해소된 뒤 정상 분석·비교까지 끝나면 목록에서 정리됩니다.</p>
            <ul className="mt-4 max-h-96 space-y-3 overflow-y-auto overscroll-contain pr-1">
                {query.data.items.map(item => <li key={item.id} className="rounded-lg border border-amber-100 bg-white p-3 break-words dark:border-amber-900 dark:bg-gray-900">
                    <h3 className="font-semibold text-gray-900 dark:text-gray-100">{item.title}</h3>
                    <p className="mt-1">{item.note}</p>
                    <p className="mt-2 text-gray-700 dark:text-gray-300"><span className="font-medium">다음 단계: </span>{item.nextStep}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
                        {item.sourceUrl && <a href={item.sourceUrl} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center text-indigo-700 underline dark:text-indigo-300">등록 출처 열기<span className="sr-only"> · {item.title} · 새 창</span></a>}
                        <span className="text-xs text-gray-500 dark:text-gray-400">최근 보류 확인 {formatStoredUtcTime(item.lastSeenAt)}</span>
                    </div>
                </li>)}
            </ul>
            {query.data.items.length === 0 && <p className="mt-3">이 페이지의 보류 기록은 정리되었습니다. 이전 페이지에서 남은 기록을 확인해주세요.</p>}
            {(offset > 0 || query.data.hasMore) && <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="보류 기록 페이지">
                <button type="button" className={button} disabled={query.isFetching || offset === 0} onClick={() => setOffset(Math.max(0, offset - POLICY_REVIEW_PAGE_SIZE))}>이전 50건</button>
                <button type="button" className={button} disabled={query.isFetching || !query.data.hasMore} onClick={() => setOffset(offset + POLICY_REVIEW_PAGE_SIZE)}>다음 50건</button>
                <span>{query.data.items.length ? `${offset + 1}–${offset + query.data.items.length} / ${query.data.total}건` : `총 ${query.data.total}건`}</span>
            </div>}
        </details>}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
            <button type="button" className={button} disabled={query.isFetching} onClick={() => query.refetch()}>보류 기록 새로고침</button>
            <span className="text-xs text-gray-600 dark:text-gray-300">저장된 기록만 읽습니다. AI를 호출하지 않습니다.</span>
        </div>
    </section>;
}

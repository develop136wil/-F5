import React from 'react';
import {AlertTriangle, Loader2} from 'lucide-react';

// Query errors are never an empty-result state. Keep stale data labelled and
// use a fixed, safe message instead of echoing transport/provider exceptions.
export default function QueryStatusNotice({query, label}) {
    if (!query.isError) return null;
    const hasPrevious = query.data !== undefined;
    return <div role="status" className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
        <AlertTriangle aria-hidden="true" className="h-4 w-4 shrink-0" />
        <div className="min-w-0 flex-1">
            <p className="font-medium">{label} 조회에 실패했습니다.</p>
            <p className="mt-1">{hasPrevious ? '이전에 불러온 자료를 표시하고 있습니다. 최신 상태는 다시 확인해주세요.' : '자료가 없는지 확인할 수 없습니다. 서버 연결을 확인한 뒤 다시 시도해주세요.'}</p>
        </div>
        <button type="button" disabled={query.isFetching} onClick={() => query.refetch()} aria-label={`${label} 다시 시도`}
            className="inline-flex min-h-10 items-center gap-2 rounded-lg px-3 font-medium underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50">
            {query.isFetching && <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />}다시 시도
        </button>
    </div>;
}

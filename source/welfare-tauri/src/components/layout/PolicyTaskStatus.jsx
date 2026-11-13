import React from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../../api/welfare';
import { fetchPolicyTaskStatuses, policyTaskPresentation } from '../../utils/policyTaskPresentation';

export default function PolicyTaskStatus() {
    const query = useQuery({
        queryKey: ['global-policy-task-status'],
        queryFn: ({ signal }) => fetchPolicyTaskStatuses(welfareApi.getPolicyTaskStatuses, signal),
        refetchInterval: query => query.state.data?.some(task => task.running) ? 2000 : 10000,
        refetchIntervalInBackground: false,
        retry: false,
    });
    const task = policyTaskPresentation(query.data);
    if (!task && !query.isError) return null;
    return <div className="rounded-xl border border-indigo-100 bg-indigo-50/60 p-3 space-y-2 text-xs text-indigo-800">
        <p role="status" className="font-semibold">{query.isError ? '정책 진행 상태 확인 필요' : task.label}</p>
        {task && !query.isError && <>
            {task.percent !== null && <div role="progressbar" aria-label={task.label} aria-valuenow={task.percent} aria-valuemin={0} aria-valuemax={100} className="h-1.5 rounded-full bg-indigo-100 overflow-hidden"><div className="h-full bg-indigo-500" style={{ width: `${task.percent}%` }} /></div>}
            <p>{task.percent === null ? '진행 중' : `${task.checked}/${task.total}건`}</p>
            <p className="break-words text-gray-600">{task.message}</p>
        </>}
        {query.isError && <p>완료 여부를 확인하지 못했습니다.</p>}
        <Link className="inline-block underline" to="/policy">정책 화면 보기</Link>
    </div>;
}

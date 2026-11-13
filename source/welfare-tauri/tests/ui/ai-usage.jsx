import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import AiUsageStatus, {PaidApiHelp} from '../../src/components/AiUsageStatus';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';
const mode = new URLSearchParams(location.search).get('mode') || 'idle';
for (const key of Object.keys(welfareApi)) if (typeof welfareApi[key] === 'function') welfareApi[key] = async () => {throw Error('실제 API 호출 금지');};
welfareApi.getAiStatus = async () => {
    window.aiStatusFetched = true;
    if (mode === 'error') throw Error('모의 조회 실패');
    return {recorded_requests_today:42, usage_date:'2026-09-06', usage_timezone:'America/Los_Angeles',
        local_budget_exhausted:mode==='legacy-local',daily_request_limit:mode==='legacy-local'?20:null,
        state_warning:mode === 'recovery' ? 'AI 사용량 기록 복구로 오늘의 요청을 보류했습니다.' : null,
        models: {model: {waiting:mode === 'waiting' ? 2 : 0, cooldown_seconds:mode === 'waiting' || mode === 'cooldown' ? 25 : 0, daily_exhausted:mode === 'daily'},...(mode==='wildcard'?{'*':{daily_exhausted:true}}:{})}};
};
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>
    <main className="mx-auto max-w-xl p-4"><AiUsageStatus/><PaidApiHelp compact/></main>
</QueryClientProvider></MemoryRouter>);

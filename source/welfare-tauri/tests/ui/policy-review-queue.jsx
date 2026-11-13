import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import PolicyReviewQueue from '../../src/components/PolicyReviewQueue';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

window.fixtureCalls = [];
window.fixtureFailure = new URLSearchParams(location.search).get('mode') === 'error';
for(const key of Object.keys(welfareApi)) if(typeof welfareApi[key] === 'function') welfareApi[key] = async () => {throw Error(`실제 호출 금지: ${key}`);};
welfareApi.getPolicyReviews = async (dbKeys, offset, signal) => {
    window.fixtureCalls.push({method:'getPolicyReviews', dbKeys, offset});
    if(signal?.aborted) throw new Error('취소됨');
    if(window.fixtureFailure) throw new Error('격리된 연결 실패');
    const total = dbKeys[0] === 'empty' ? 0 : 53;
    const items = Array.from({length:total},(_,index)=>({
        id:`fixture-${index}`,policy_id:index+1,status:'unresolved',title:`정책 ${index+1} · 출생일과 나이 기준`,
        note:'출생연도와 신청일 기준의 적용 관계를 확인해야 합니다.',code:'source_criteria_review_required',
        source_url:index===0?'javascript:alert(1)':'https://example.org/public/policy',last_seen_at:'2026-09-13T01:00:00Z',
    })).slice(offset,offset+50);
    return {items,total,truncated:offset+items.length<total};
};
function App() {
    const [scope,setScope]=useState('fixture');
    return <main className="min-h-screen bg-[#F2F4F6] p-4 text-gray-900 dark:bg-gray-900 dark:text-gray-100">
        <h1 className="mb-4 text-2xl font-semibold">정책 보류 기록 격리 검사</h1>
        <button className="mb-4 min-h-11 rounded-lg border px-3" onClick={()=>setScope(scope==='fixture'?'empty':'fixture')}>다른 정책표 선택</button>
        <PolicyReviewQueue key={scope} dbKeys={[scope]}/>
    </main>;
}
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><App/></QueryClientProvider>);

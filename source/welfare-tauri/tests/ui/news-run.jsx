import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '../../src/index.css';
import { welfareApi } from '../../src/api/welfare';
import NewsProfileRunPanel from '../../src/components/NewsProfileRunPanel';

const client = new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});

welfareApi.getNewsWorkflowOptions = async () => ({enabled:true,key_ready:true,semantic_calibrated:true,
    ai_required:true, selection:{profile_id:'fixture',revision:3,name:'노년·한부모 지원',region:'전국',
        min_age_months:null,max_age_months:null,life_cycles:['노년'],characteristics:['한부모·조손'],
        sources:['sites','bokjiro'],sites:[{id:1,name:'도봉구청'}]}});
const workflows = [
    {workflow_id:'1',run_id:'run-1',state:'waiting',stage:'collecting',active:false,
        summary:{collection:{state:'busy',sources:{sites:{state:'finished',registered:8},bokjiro:{state:'busy'}}}},
        updated_at:'2026-09-01 00:00:00'},
    {workflow_id:'2',state:'cancelled',stage:'cancelled',active:false,updated_at:'2026-09-01 00:00:00'}];
if(new URLSearchParams(location.search).get('mode')==='vision'){
    workflows.splice(0,workflows.length,{workflow_id:'vision',run_id:'vision-run',revision:3,
        state:'complete',stage:'finished',active:false,can_resume:false,
        summary:{collection:{state:'finished',registered:0,vision_review_sites:2,
            sources:{sites:{state:'finished',registered:0,saved_total:0,vision_review_sites:2}}}},
        updated_at:'2026-09-09 00:00:00'});
}
globalThis.__fixtureNewsRunWrites=0;
globalThis.__fixtureSetVisionReview=count=>{
    workflows[0].summary.collection.vision_review_sites=count;
    workflows[0].summary.collection.sources.sites.vision_review_sites=count;
};
welfareApi.getNewsWorkflows = async () => ({items:structuredClone(workflows)});
welfareApi.getNewsWorkflow = async () => ({});
welfareApi.startNewsWorkflow = async () => {globalThis.__fixtureNewsRunWrites++; throw Error('비활성 상태에서 호출되면 안 됩니다.'); };
welfareApi.cancelNewsWorkflow = async () => {globalThis.__fixtureNewsRunWrites++;return {};};
welfareApi.resumeNewsWorkflow = async id => {
    globalThis.__fixtureNewsRunWrites++;
    const item = workflows.find(value=>value.workflow_id===id);
    item.state='complete'; item.stage='finished';
    item.summary.collection={state:'finished',registered:11,
        sources:{sites:{state:'finished',registered:8},bokjiro:{state:'finished',registered:3}}};
    return structuredClone(item);
};
welfareApi.getAiStatus = async () => ({busy:false});

createRoot(document.getElementById('root')).render(<QueryClientProvider client={client}><main className="mx-auto max-w-5xl p-6">
    <p className="mb-4 text-sm">격리 UI 테스트 · 모의 상태만 사용 · 실제 수집/AI 연결 없음</p>
    <NewsProfileRunPanel profile={{profile_id:'fixture',name:'노년·한부모 지원',revision:3}} onClose={() => {}} />
</main></QueryClientProvider>);

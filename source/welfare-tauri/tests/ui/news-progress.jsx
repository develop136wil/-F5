import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,Routes,Route} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import Layout from '../../src/components/layout/Layout';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import NotionUpload from '../../src/pages/NotionUpload';
import '../../src/index.css';

const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
globalThis.__unexpected=[];globalThis.__reads=0;globalThis.__optionReads=0;globalThis.__feedReads=[];
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{globalThis.__unexpected.push(key);throw Error('격리 시험: 실제 호출 차단 '+key);};
const profile={profile_id:'default-news',name:'도봉구 아동 새 소식',revision:1,region:'서울특별시 도봉구',life_cycles:['아동'],characteristics:[],min_age_months:0,max_age_months:108,publication_period:'30'};
let task={workflow_id:'progress-fixture',run_id:'fixture-run',profile_id:profile.profile_id,revision:1,state:'running',stage:'collecting',active:true,summary:{},
    progress:{schema_version:1,phase:'sites',site_name:'시험 기관 공지사항',sites_processed:12,sites_total:42,sites_review_required:1,documents_processed:25,attachments_processed:3}};
let fail=false,failCondition=false;
globalThis.__task=()=>structuredClone(task);
globalThis.__serverTask=patch=>{task={...task,...patch};};
globalThis.__setTask=async patch=>{task={...task,...patch};await client.invalidateQueries({queryKey:['global-news-task-status']});};
globalThis.__failRead=async()=>{fail=true;await client.invalidateQueries({queryKey:['global-news-task-status']});};
globalThis.__failCondition=async()=>{failCondition=true;await client.invalidateQueries({queryKey:['defaultNewsCondition']});};
globalThis.__restoreCondition=async()=>{failCondition=false;await client.invalidateQueries({queryKey:['defaultNewsCondition']});};
welfareApi.getStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
for(const key of ['getPolicyFullCheckStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRestyleStatus','getPolicyRecomputeStatus','getPolicyStatus'])welfareApi[key]=async()=>({running:false});
welfareApi.getPolicyTaskStatuses=async()=>['정책 최신화','Notion 불러오기','정책 비교','스타일 제안 생성','변경 제안 다시 확인','출처 변경 확인'].map(label=>({label,running:false}));
welfareApi.getNewsProfiles=async()=>({items:[profile]});
welfareApi.getNewsWorkflows=async()=>{globalThis.__reads++;if(fail)throw Error('합성 연결 실패');return {items:[task]};};
welfareApi.getNewsWorkflow=async()=>{globalThis.__reads++;if(fail)throw Error('합성 연결 실패');return task;};
welfareApi.getNewsWorkflowOptions=async()=>{globalThis.__optionReads++;return {enabled:true,selection:profile};};
welfareApi.getDefaultNewsCondition=async()=>{if(failCondition)throw Error('합성 조건 조회 실패');return {profile};};
welfareApi.getNewsFeed=async({view})=>{
    globalThis.__feedReads.push(view);
    const items=view==='review'?Array.from({length:4},(_,index)=>({id:`held-${index}`,title:`확인이 필요한 자료 ${index+1}`,status:'직접 확인 필요',selectable:false})):[];
    return {items,total:items.length,has_more:false,configured:false,counts:{ready:0,review:4,processing:0,records:0}};
};

createRoot(document.getElementById('root')).render(<MemoryRouter initialEntries={['/notion']}><QueryClientProvider client={client}><ScrapeProvider><Layout>
    <p className="mb-4 text-xs text-gray-500">격리 시험 · 실제 DB·AI·Notion 호출 없음</p>
    <Routes><Route path="/notion" element={<NotionUpload/>}/><Route path="*" element={<h1>다른 화면 시험</h1>}/></Routes>
</Layout></ScrapeProvider></QueryClientProvider></MemoryRouter>);

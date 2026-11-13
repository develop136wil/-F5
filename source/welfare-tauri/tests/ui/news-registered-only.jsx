import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {welfareApi} from '../../src/api/welfare';
import NotionUpload from '../../src/pages/NotionUpload';
import '../../src/index.css';
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const mode=new URLSearchParams(location.search).get('mode');
window.fixtureCalls=[];
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{window.fixtureCalls.push(name);throw Error('Unexpected fixture request: '+name);};
const profile={profile_id:'default-news',revision:4,name:'기본 수집'};
const history=Array.from({length:12},(_,i)=>({workflow_id:`old-${i}`,run_id:`old-run-${i}`,revision:3,state:'waiting',stage:'collecting',can_resume:false,resume_block_reason:'news_source_scope_changed',summary:{collection:{sources:{bokjiro:{state:'finished',registered:596,total_reported:600}}}}}));
const partial={workflow_id:'new',run_id:'new-run',revision:4,state:'complete',stage:'finished',can_resume:false,
 summary:{collection:{state:'finished',site_review_required:1,site_issues:[{site_id:'1',site_name:'시험 기관 A',issue_code:'site_read_failed'}],vision_review_sites:1,
 sources:{sites:{state:'finished',saved_total:2,site_review_required:1,vision_review_sites:1}}}}};
welfareApi.getStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getDefaultNewsCondition=async()=>({profile});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,ai_required:true,analysis_ready:true,selection:{...profile,region:'서울특별시 도봉구',life_cycles:['영유아','아동'],characteristics:[],min_age_months:0,max_age_months:108,sources:['sites'],sites:[{id:'1',name:'시험 기관 A'},{id:'2',name:'시험 기관 B'}]}});
welfareApi.getNewsWorkflows=async()=>({items:mode==='partial'?[partial,...history]:history});
welfareApi.getNotionCandidates=async()=>({configured:true,items:[{id:1,title:'시험 기관 B 교육 프로그램 안내',org:'시험 기관 B',status:'진행 중',link:'https://example.invalid/notice/1'},{id:2,title:'시험 기관 B 가족 지원 소식',org:'시험 기관 B',status:'진행 예정',link:'https://example.invalid/notice/2'}]});
welfareApi.getNewsProfileResults=async(id,offset,runId)=>{window.fixtureCalls.push('results:'+runId);return {items:[],has_more:false};};
welfareApi.getNewsProfiles=async()=>({items:[]});
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><ScrapeProvider><main className="mx-auto max-w-6xl bg-gray-100 p-4 text-gray-800"><NotionUpload/></main></ScrapeProvider></QueryClientProvider></MemoryRouter>);

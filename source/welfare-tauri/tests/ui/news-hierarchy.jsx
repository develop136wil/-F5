import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import {Toaster} from 'sonner';
import {welfareApi} from '../../src/api/welfare';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {queryKeys} from '../../src/api/queryKeys';
import '../../src/index.css';
const mode=new URLSearchParams(location.search).get('mode');
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const sample=[
    {id:1,title:'지역 가족과 아동을 위한 교육·문화 체험 및 신청 안내 — 지원 대상과 기간을 함께 확인하는 매우 긴 새 소식 제목입니다',org:'서울특별시 도봉구 가족문화교육지원기관',status:'진행 예정',start:'2026-09-15',end:'2026-09-20',link:'https://example.org/news/one',input_review_required:true,input_note:'본문 일부를 읽지 못했습니다. 지원 대상과 제외 조건을 원문에서 확인해주세요.',attachment_review_required:true,attachment_note:'첨부 안내문을 직접 확인해주세요.'},
    {id:2,title:'공백없이길게작성된교육문화복지소식제목'.repeat(6),org:'기관명이매우길게이어지는경우에도줄바꿈을유지하는시험기관',status:'진행 중',start:'2026-09-01',end:'2026-10-31',link:'https://example.org/news/two',upload_uncertain:true},
    {id:3,title:'짧은 소식',org:'시험 기관',status:'종료',start:'2026-08-01',end:'2026-08-10',link:'https://example.org/news/three'},
];
window.newsFixture={rows:structuredClone(sample),calls:{detail:[],upload:[],html:[],dismiss:[],retry:[],unexpected:[]},phase:mode==='pending'?'pending':'ready'};
const state=window.newsFixture;
let resolveList;
window.finishNewsList=()=>resolveList?.();
window.newsPhase=async phase=>{state.phase=phase;if(phase==='empty')state.rows=[];if(phase==='ready')state.rows=structuredClone(sample);await client.invalidateQueries({queryKey:queryKeys.newsFeed});};
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{state.calls.unexpected.push(name);throw Error(`외부 호출 차단: ${name}`);};
welfareApi.getNotionCandidates=async()=>{if(state.phase==='pending')await new Promise(resolve=>{resolveList=()=>{state.phase='ready';resolve();};});if(state.phase==='error')throw Error('모의 목록 오류');return {configured:true,count:999,items:structuredClone(state.rows)};};
welfareApi.getNewsFeed=async({view='ready',offset=0}={})=>{
    const result=await welfareApi.getNotionCandidates();
    const rows=view==='ready'?result.items.map(item=>({...item,item_id:item.id,id:`item:${item.id}`,fingerprint:String(item.id).padStart(64,'0'),selectable:true})):[];
    return {configured:true,total:rows.length,count:999,items:rows.slice(offset,offset+10),offset,limit:10,has_more:offset+10<rows.length,counts:{ready:state.rows.length,processing:0,review:0,records:0}};
};
welfareApi.prepareNewsFeedAction=async(_profileId,_action,selected)=>({ids:selected.map(item=>Number(item.id.split(':')[1])),ai_called:false,notion_uploaded:false});
welfareApi.getStatus=async()=>({is_running:false});
welfareApi.getConfig=async()=>({is_configured:true,TARGET_REGION:'도봉구',TARGET_AGE_MAX_MONTHS:108});
welfareApi.getDefaultNewsCondition=async()=>({profile:mode==='profile'?{profile_id:'default-news',revision:1,name:'기본 수집'}:null,legacy_collection_available:true});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,ai_required:true,selection:{revision:1,name:'기본 수집',region:'서울특별시 도봉구',life_cycles:['아동'],characteristics:[],sources:['sites'],sites:[{id:'1',name:'시험 기관'}],min_age_months:0,max_age_months:108}});
welfareApi.getNewsWorkflows=async()=>({items:[]});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getSites=async()=>[{id:1,name:'시험 기관'}];
welfareApi.getAiBacklogStatus=async()=>({backlog_total:0});
welfareApi.getItemDetail=async id=>{state.calls.detail.push(id);return {...state.rows.find(item=>item.id===id),support_detail:'시험용 상세 내용',ai_data:{}};};
welfareApi.getSimilarItems=async()=>[];
welfareApi.uploadToNotion=async ids=>{state.calls.upload.push(ids);return {total:ids.length,uploaded:ids.length};};
welfareApi.exportNewsGallery=async ids=>{state.calls.html.push(ids);return {posters:0,cacheHits:0};};
welfareApi.dismissNotionCandidates=async ids=>{state.calls.dismiss.push(ids);state.rows=state.rows.filter(item=>!ids.includes(item.id));return {};};
welfareApi.allowNotionUploadRetry=async ids=>{state.calls.retry.push(ids);return {};};
const {default:NotionUpload}=await import('../../src/pages/NotionUpload');
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><ScrapeProvider><main className="mx-auto max-w-6xl bg-gray-100 p-4 text-gray-800 dark:bg-gray-900 dark:text-gray-100"><NotionUpload/></main><Toaster/></ScrapeProvider></QueryClientProvider></MemoryRouter>);

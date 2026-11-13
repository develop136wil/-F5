// Real pages/provider, synthetic data only. All production API methods are replaced.
import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Routes, Route, Link, useLocation, useNavigate} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import '../../src/index.css';
import Home from '../../src/pages/Home';
import Search from '../../src/pages/Search';
import NotionUpload from '../../src/pages/NotionUpload';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {welfareApi} from '../../src/api/welfare';
import {queryKeys} from '../../src/api/queryKeys';

const client = new QueryClient({defaultOptions: {queries: {retry:false, refetchOnWindowFocus:false}}});
const collectionWithoutAi=new URLSearchParams(location.search).get('mode')==='collection-no-ai';
let notify = () => {}, status = {is_running:false,progress:0}, ai = {busy:false};
let startResponse, stopResponse, exportResponse, failNext = false;
let failCounts = false;
let rows = [{id:1,title:'시험용 가족 체험 행사',status:'진행 예정',input_review_required:true,input_note:'포스터의 일부 내용을 확인하지 못했습니다.'}];
const counts = {start:0,stop:0,candidates:0,html:0,upload:0};
for(const key of Object.keys(welfareApi)) if(typeof welfareApi[key] === 'function') welfareApi[key] = async () => {throw Error('격리 시험: 실제 API 호출 차단');};
welfareApi.getStatus = async () => status;
welfareApi.getAiStatus = async () => ai;
welfareApi.getConfig = async () => ({revision:'fixture',is_configured:!collectionWithoutAi,TARGET_REGION:'도봉',TARGET_AGE_MAX_MONTHS:108,notion_token_configured:true});
welfareApi.getDefaultNewsCondition = async () => ({profile:collectionWithoutAi?{profile_id:'default-news',revision:1,name:'현재 수집 조건'}:null,legacy_collection_available:true});
welfareApi.getNewsWorkflows=async()=>({items:[]});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,ai_required:true,analysis_ready:false,analysis_reason_code:'authentication_required',analysis_reason:'AI 연결 없이 원문을 먼저 수집할 수 있습니다.',selection:{revision:1,name:'현재 수집 조건',region:'도봉',life_cycles:[],characteristics:[],sources:['sites'],sites:[{id:'1',name:'시험 기관'}],min_age_months:null,max_age_months:null}});
window.workflowStarts=[];
welfareApi.startNewsWorkflow=async(...args)=>{window.workflowStarts.push(args);return {};};
welfareApi.verifyGoogleApiKey = async () => ({revision:'fixture',ok:true});
welfareApi.getSites = async () => [{id:1,name:'시험 기관'}];
welfareApi.getChangedItems = async () => ({items:[]});
welfareApi.getExpiringItems = async () => ({total:1,items:[{id:1,title:'시험 마감 소식',deadline:'2026-09-14',link:'https://example.invalid/event'}]});
welfareApi.getNewsProfiles = async () => ({items:[]});
welfareApi.getPolicyChanges = async () => {if(failCounts) throw Error('모의 조회 실패');return {items:Array.from({length:115},(_,id)=>({id}))};};
welfareApi.getSummaryStats = async () => ({by_category:{생활지원:1}});
welfareApi.searchItems = async () => ({items:[],total:0});
welfareApi.semanticSearch = async () => ({items:[],total:0});
welfareApi.getAiBacklogStatus = async () => ({backlog_total:0});
for(const key of ['getPolicyFullCheckStatus','getPolicySyncStatus','getPolicyAuditStatus',
    'getPolicyRestyleStatus','getPolicyRecomputeStatus','getPolicyStatus']) welfareApi[key] = async () => ({running:false});
welfareApi.getPolicyTaskStatuses=async()=>['정책 최신화','Notion 불러오기','정책 비교','스타일 제안 생성','변경 제안 다시 확인','출처 변경 확인'].map(label=>({label,running:false}));
welfareApi.getNotionCandidates = async () => {counts.candidates++;notify();return {configured:false,items:rows,count:rows.length};};
welfareApi.startScrape = async () => {
    counts.start++;notify();await new Promise((resolve,reject)=>{startResponse=()=>{startResponse=null;if(failNext){failNext=false;reject(Error('시험용 시작 실패'));}else{status={is_running:true,progress:40,message:'시험 자료 확인 중'};resolve();}notify();};notify();});
};
welfareApi.stopScrape = async () => {counts.stop++;notify();await new Promise(resolve=>{stopResponse=()=>{stopResponse=null;status={is_running:false,progress:40,status_code:'stopped'};resolve();notify();};notify();});};
welfareApi.exportNewsGallery = async () => {counts.html++;notify();await new Promise(resolve=>{exportResponse=()=>{exportResponse=null;resolve();notify();};notify();});return {posters:0,cacheHits:1};};
welfareApi.uploadToNotion = async () => {counts.upload++;notify();throw Error('실제 등록 차단');};
// Layout captures policy polling functions on import; load it after the mocks.
const {default: Layout} = await import('../../src/components/layout/Layout');
// Only this isolated localhost origin. Never production config.
localStorage.setItem('welfare-f5-welcome-seen','true');
localStorage.setItem('welfare-f5-home-setup-card-collapsed',collectionWithoutAi?'false':'true');
localStorage.removeItem('welfare-f5-target-reviewed');

function Fixture() {
    const [,update] = useState(0), [width,setWidth] = useState(collectionWithoutAi?390:900), [mount,setMount] = useState(0);
    const location = useLocation(), navigate = useNavigate();
    useEffect(()=>{notify=()=>update(n=>n+1);return()=>{notify=()=>{};};},[]);
    return <>
        <aside className="space-y-2 border-b bg-white p-3 text-sm" aria-label="시험 제어">
            <p>가짜 데이터 시험 · 실제 수집/AI/Notion 호출 없음</p>
            <output aria-label="시험 요청 횟수">{JSON.stringify(counts)}</output><p>현재 화면: {location.pathname}{location.search}</p>
            <div className="flex flex-wrap gap-3">
                {[582,850,900,1280,1478].map(n=><button key={n} onClick={()=>setWidth(n)}>{n}px</button>)}
                <button onClick={()=>{failCounts=true;client.invalidateQueries({queryKey:['dashboardPolicyChanges']});}}>건수 갱신 실패 시험</button>
                <button onClick={()=>{failCounts=false;client.invalidateQueries({queryKey:['dashboardPolicyChanges']});}}>건수 조회 복구</button>
                <button onClick={()=>startResponse?.()} disabled={!startResponse}>시작 응답 완료</button>
                <button onClick={()=>stopResponse?.()} disabled={!stopResponse}>중지 응답 완료</button>
                <button onClick={()=>exportResponse?.()} disabled={!exportResponse}>저장 응답 완료</button>
                <button onClick={()=>{failNext=true;notify();}}>다음 시작 실패</button>
                <button onClick={()=>{rows=[...rows,{id:2,title:'새로 찾은 시험 행사',status:'진행 예정'}];status={is_running:false,progress:100,status_code:'completed',ai_savings:{analyzed:1,saved_total:0}};notify();}}>수집 완료·후보 추가</button>
                <button onClick={()=>{ai=ai.busy?{busy:false}:{busy:true,job_label:'정책 최신화'};client.setQueryData(['ai-global-status'],ai);}}>다른 AI 작업 전환</button>
                <button onClick={()=>{localStorage.removeItem('welfare-f5-home-setup-card-collapsed');navigate('/');setMount(n=>n+1);}}>설정 안내 펼침 시험</button>
            </div>
        </aside>
        <div id="fixture-app" style={{width,maxWidth:'none'}}>
            <Layout><Routes><Route path="/" element={<Home key={mount}/>} /><Route path="/notion" element={<NotionUpload/>}/><Route path="/search" element={<Search/>}/>
                <Route path="*" element={<div><h1>이동한 화면: {location.pathname}{location.search}</h1><Link to="/">홈으로 돌아가기</Link></div>}/>
            </Routes></Layout>
        </div><Toaster />
    </>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><ScrapeProvider><Fixture/></ScrapeProvider></QueryClientProvider></MemoryRouter>);

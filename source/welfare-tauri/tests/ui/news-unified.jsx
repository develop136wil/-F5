import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {welfareApi} from '../../src/api/welfare';
import NotionUpload from '../../src/pages/NotionUpload';
import NewsPublicationPeriod from '../../src/components/NewsPublicationPeriod';
import {publicationSettings} from '../../src/utils/newsPublication';
import {queryKeys} from '../../src/api/queryKeys';
import '../../src/index.css';

const queryClient=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const mode=new URLSearchParams(location.search).get('mode') || 'default';
const legacy=['initial','legacy-ready','legacy-partial'].includes(mode);
const hasRecords=['default','empty-records','feed-error','unknown-counts'].includes(mode);
for(const key of Object.keys(welfareApi)) if(typeof welfareApi[key]==='function') welfareApi[key]=async()=>{throw Error('격리시험: 실제 API 차단 '+key);};
const profile={profile_id:'default-news',name:'현재 수집 조건',revision:1,region:'서울특별시 도봉구',life_cycles:['아동'],characteristics:[],min_age_months:0,max_age_months:108,publication_period:'30'};
const row=i=>({id:'job:'+i,record_id:'job:'+i,job_id:String(i),profile_id:'default-news',fingerprint:'fingerprint-'+i,
    title:`소식 ${i} · 아동 교육·돌봄 프로그램 참여 안내`,link:'https://example.invalid/post/'+i,org:'시험 복지기관',
    published_date:i===3?null:'2026-09-17',status:'진행 중',target:'도봉구 거주 아동과 보호자',support_preview:'교육·돌봄 프로그램 참여를 지원합니다.',
    selectable:true,view:'ready',state:'complete',upload_uncertain:i===2,review_required:i===1});
let rows=Array.from({length:['initial','empty','empty-records','feed-error','unknown-counts','legacy-partial'].includes(mode)?0:mode==='default'?105:3},(_,i)=>row(i+1));
const calls=globalThis.__calls={prepare:[],upload:[],html:[],dismiss:[],retry:[],details:[],feed:[],verify:[]};
globalThis.__setRowCount=async count=>{rows=Array.from({length:count},(_,i)=>row(i+1));await queryClient.invalidateQueries({queryKey:queryKeys.newsFeed});};
let holdPrepare=false, releasePrepare;
globalThis.__holdPrepare=()=>{holdPrepare=true;};
globalThis.__finishPrepare=()=>releasePrepare?.();
welfareApi.getStatus=async()=>mode==='legacy-partial'
    ? {is_running:false,status_code:'partial_failure',message:'원문은 보존했으며 분석 1건을 마치지 못했습니다.',ai_savings:{analysis_failed:1}}
    : {is_running:false};
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getDefaultNewsCondition=async()=>({profile:legacy?null:profile,legacy_collection_available:legacy});
welfareApi.getConfig=async()=>({is_configured:true,TARGET_REGION:'서울특별시 도봉구',TARGET_AGE_MAX_MONTHS:108});
welfareApi.getSites=async()=>mode==='initial'?[]:[{id:1,name:'시험 기관 게시판',url:'https://example.invalid/board'}];
welfareApi.getAiBacklogStatus=async()=>({backlog_total:0,permanently_excluded:0});
welfareApi.verifyNotion=async()=>{calls.verify.push(true);return {ok:true,title:'격리 시험 보관함'};};
welfareApi.getNewsProfiles=async()=>({items:[{...profile,profile_id:'other',name:'저장한 다른 조건'}]});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,selection:profile});
welfareApi.getNewsWorkflows=async()=>({items:[{workflow_id:'fixture-workflow',run_id:'fixture-run',state:'complete',revision:1,summary:{}}]});
welfareApi.getNewsFeed=async params=>{
    calls.feed.push(params);
    if(mode==='feed-error'&&params.view==='ready')throw Error('격리 시험: 목록 조회 실패');
    const filtered=params.view==='ready'?rows:hasRecords&&params.view==='review'?[{...row(201),state:'failed',selectable:false,review_required:true,input_note:'원문에 개인정보가 있는지 확인이 필요합니다.'}]:hasRecords&&params.view==='processing'?[{...row(202),state:'queued',selectable:false,review_required:false,processing_required:true,input_note:'아직 분석을 시작하지 않은 자료입니다.'}]:[];
    const items=filtered.slice(params.offset,params.offset+10);
    return {items,total:filtered.length,count:items.length,has_more:params.offset+10<filtered.length,configured:mode!=='disconnected',offset:params.offset,limit:10,counts:mode==='unknown-counts'?undefined:{ready:rows.length,review:hasRecords?1:0,processing:hasRecords?1:0,records:0}};
};
welfareApi.prepareNewsFeedAction=async(profileId,action,items)=>{
    calls.prepare.push({profileId,action,items});
    if(holdPrepare)await new Promise(resolve=>{releasePrepare=resolve;});
    return {ids:items.map(item=>Number(item.id.split(':')[1])),ai_called:false,notion_uploaded:false};
};
welfareApi.uploadToNotion=async ids=>{calls.upload.push(ids);return{total:ids.length,uploaded:ids.length};};
welfareApi.exportNewsGallery=async ids=>{calls.html.push(ids);return{posters:0,cacheHits:0};};
welfareApi.dismissNotionCandidates=async ids=>{calls.dismiss.push(ids);rows=rows.filter(row=>!ids.includes(Number(row.id.split(':')[1])));};
welfareApi.allowNotionUploadRetry=async ids=>{calls.retry.push(ids);};
welfareApi.getNewsResultDetails=async(profileId,recordId)=>{
    calls.details.push({profileId,recordId});
    return {record_id:recordId,ai_called:false,details:{target:'도봉구 거주 아동과 보호자',support_detail:'교육비와 돌봄 프로그램을 지원합니다.\n신청 기간: 9월 20일부터 30일까지',eligibility_detail:'기관 안내에 따른 자격 조건',contact:'시험기관 대표전화 02-0000-0000'}};
};
function Fixture(){
    const [show,setShow]=useState(true),[period,setPeriod]=useState({publication_period:'30'}),[validation,setValidation]=useState('');
    globalThis.__leave=()=>setShow(false);
    return <main className="mx-auto max-w-5xl space-y-6 p-5"><p className="text-xs text-gray-500">격리 시험 · 실제 DB·AI·Notion 호출 없음</p>
        {show&&<NotionUpload/>}<section aria-label="기간 시험" className="rounded-xl bg-white p-4"><NewsPublicationPeriod value={period} onChange={(key,value)=>setPeriod(current=>({...current,[key]:value}))}/>
        <button onClick={()=>{try{setValidation(JSON.stringify(publicationSettings(period)));}catch(e){setValidation(e.message);}}}>기간 저장 시험</button><output aria-label="기간 저장 결과">{validation}</output></section><Toaster/></main>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={queryClient}><ScrapeProvider><Fixture/></ScrapeProvider></QueryClientProvider></MemoryRouter>);

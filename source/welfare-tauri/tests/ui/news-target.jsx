import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import NewsTargetCollection from '../../src/components/NewsTargetCollection';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
let notify=()=>{},ready=false,pending=null,workflow=null,fail=false,importPending=null,imported=false,importFail=false;
const counts={list:0,options:0,start:0,resume:0,cancel:0,results:0,previewImport:0,import:0};
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{throw Error('실제 API 호출 차단');};
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getNewsProfiles=async()=>{counts.list++;notify();return {items:[{profile_id:'p',revision:1,name:'노년 복지 소식'}]};};
welfareApi.getNewsWorkflowOptions=async()=>{counts.options++;notify();return {enabled:ready,reason:ready?'':'품질 확인 기준이 준비되지 않았습니다.',selection:{name:'노년 복지 소식',revision:2,region:'서울',min_age_months:null,max_age_months:null,life_cycles:['노년'],characteristics:[],sites:[{id:'1',name:'시험 기관'}]}};};
welfareApi.getNewsWorkflows=async()=>({items:workflow?[workflow]:[]});
welfareApi.getNewsWorkflow=async()=>({...workflow});
welfareApi.startNewsWorkflow=async(id,revision)=>{
    if(revision!==2)throw Error('오래된 목록 버전으로 시작함');
    counts.start++;notify();
    await new Promise((resolve,reject)=>{pending=()=>{pending=null;if(fail){fail=false;reject(Error('모의 시작 실패'));}else{workflow={workflow_id:'w1',run_id:'r1',state:'waiting',stage:'analyzing',active:false};resolve();}notify();};notify();});
    return {...workflow};
};
welfareApi.resumeNewsWorkflow=async()=>{counts.resume++;workflow={...workflow,state:'complete',stage:'finished',active:false};notify();};
welfareApi.cancelNewsWorkflow=async()=>{counts.cancel++;workflow={...workflow,state:'cancelled',active:false};notify();};
welfareApi.getNewsProfileResults=async(id,offset,runId)=>{counts.results++;notify();return {has_more:false,items:[{record_id:'j1',job_id:'j1',run_id:runId||'r1',revision:2,title:'어르신 돌봄 지원',state:'complete',target_status:'review',input_note:'첨부파일 직접 확인 필요',link:'https://example.invalid/notice',details:{support_detail:'돌봄 서비스 지원',eligibility_detail:'원문 조건 확인'}}]};};
welfareApi.previewNewsResultImport=async()=>{counts.previewImport++;notify();return {status:imported?'already_imported':'ready',title:'어르신 돌봄 지원',revision:2,note:'대상 근거 부족 · 첨부파일을 직접 확인해주세요.',fingerprint:'a'.repeat(64)};};
welfareApi.importNewsResult=async(id,jobId,fingerprint)=>{
    if(fingerprint!=='a'.repeat(64))throw Error('확인 버전 불일치');
    counts.import++;notify();
    await new Promise((resolve,reject)=>{importPending=()=>{importPending=null;if(importFail){importFail=false;reject(Error('모의 저장 실패'));}else{imported=true;resolve();}notify();};notify();});
    return {status:'created',item_id:1};
};
function Fixture(){
    const [,update]=useState(0),[width,setWidth]=useState(582),[busy,setBusy]=useState(false);
    notify=()=>update(n=>n+1);
    return <div style={{width,maxWidth:'100%'}} className="mx-auto p-4 space-y-4">
        <aside className="space-y-3 rounded-xl border p-4"><p>가짜 데이터 · 실제 수집/AI 없음</p><output>{JSON.stringify(counts)}</output>
            <div className="flex flex-wrap gap-3"><button onClick={()=>{ready=true;notify();}}>준비 조건 충족</button><button onClick={()=>{fail=true;}}>다음 시작 실패</button>
            <button disabled={!pending} onClick={()=>pending?.()}>시작 응답 완료</button><button onClick={()=>setBusy(v=>!v)}>다른 작업 전환</button><button onClick={()=>setWidth(width===582?900:582)}>시험 폭 변경</button>
            <button disabled={!importPending} onClick={()=>importPending?.()}>가져오기 응답 완료</button><button onClick={()=>{importFail=true;}}>다음 가져오기 실패</button></div>
        </aside><NewsTargetCollection actionBusy={busy}/>
    </div>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><Fixture/></QueryClientProvider></MemoryRouter>);

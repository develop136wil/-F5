import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import NewsProfileRunPanel from '../../src/components/NewsProfileRunPanel';
import ItemDetailModal from '../../src/components/ItemDetailModal';
import NewsFeedDetailDialog from '../../src/components/NewsFeedDetailDialog';
import '../../src/index.css';
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
globalThis.__unexpected=[];globalThis.__clears=[];
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{globalThis.__unexpected.push(key);throw Error('격리시험: API 차단 '+key);};
const profile={profile_id:'history-test',revision:2,name:'현재 조건',region:'전국',life_cycles:[],characteristics:[],publication_period:'30'};
let items=Array.from({length:5},(_,index)=>({workflow_id:'done-'+index,run_id:'run-'+index,state:'complete',stage:'finished',revision:2,can_resume:false,can_clear_history:true,updated_at:'2026-09-17 12:30:00',summary:{collection:{attachments_analyzed:3}},progress:{schema_version:1,phase:'analysis',analysis_processed:10,analysis_total:10,analysis_remaining:0,analysis_running:0}}));
globalThis.__waiting=()=>{items.unshift({workflow_id:'waiting',run_id:'waiting-run',state:'waiting',stage:'analyzing',revision:2,can_resume:true,can_clear_history:false,error_code:'daily_quota_exhausted',summary:{collection:{state:'finished'}}});};
globalThis.__running=()=>{items.unshift({workflow_id:'running',run_id:'running-run',state:'running',active:true,stage:'collecting',revision:2,can_resume:false,can_clear_history:false,summary:{}});};
const taskMode=new URLSearchParams(location.search).get('task');
if(taskMode==='waiting')globalThis.__waiting();
if(taskMode==='running')globalThis.__running();
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,selection:profile});
welfareApi.getNewsWorkflows=async()=>({items});
welfareApi.getNewsWorkflow=async id=>items.find(item=>item.workflow_id===id);
welfareApi.clearNewsWorkflowHistory=async(id,ids)=>{
    globalThis.__clears.push({id,ids});
    const old=items.length;items=items.filter(item=>!item.can_clear_history||(ids&&!ids.includes(item.workflow_id)));
    return {cleared:old-items.length,news_deleted:false};
};
const support='### ✅ 지원 내용\n- 프로그램 **참여**\n\n### 📋 신청 자격\n- 느린학습자 아동\n\n### 📌 신청 방법\n1. 온라인 신청\n2. 결과 확인\n\n<script>window.__unsafe=true</script>';
welfareApi.getItemDetail=async()=>({id:1,title:'기존 소식 상세',support_detail:support,target:'아동',link:'https://example.invalid/post'});
welfareApi.getSimilarItems=async()=>[];
welfareApi.getNewsResultDetails=async(_profile,record)=>({record_id:record,ai_called:false,details:{support_detail:support,target:'아동'}});
function Fixture(){
    const [detail,setDetail]=useState(null);
    return <main className="mx-auto max-w-4xl space-y-5 p-5"><NewsProfileRunPanel profile={profile} primaryLabel="새 소식 찾기" onResults={()=>{}}/>
        <button onClick={()=>setDetail('saved')}>저장 소식 상세</button><button onClick={()=>setDetail('feed')}>새 소식 상세</button>
        {detail==='saved'&&<ItemDetailModal itemId={1} onClose={()=>setDetail(null)} tone="news"/>}
        {detail==='feed'&&<NewsFeedDetailDialog item={{record_id:'job:1',profile_id:profile.profile_id,state:'complete',title:'새 소식 상세 내용'}} onClose={()=>setDetail(null)}/>}
    </main>;
}
createRoot(document.getElementById('root')).render(<QueryClientProvider client={client}><Fixture/></QueryClientProvider>);

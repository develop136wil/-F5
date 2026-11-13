// Diagnostic-only fixture. All APIs are in-memory; no credentials or external writes.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,Routes,Route,Link,useNavigate} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import {welfareApi} from '../../src/api/welfare';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import NotionUpload from '../../src/pages/NotionUpload';
import Sites from '../../src/pages/Sites';
import PolicyManage from '../../src/pages/PolicyManage';
import DefaultNewsConditionEditor from '../../src/components/DefaultNewsConditionEditor';
import '../../src/index.css';

const copy=value=>structuredClone(value);
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const state={profile:null,sites:[],workflow:null,ai:false,dbs:[],localCount:0,previewFailure:false,policyStatus:{running:false,state:'idle'}};
const calls={unexpected:[],siteAdds:[],saves:[],starts:[],previews:[],creates:[],checks:[]};
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{
    calls.unexpected.push(name);throw Error(`No real API allowed: ${name}`);
};
window.blankAudit={state,calls,refresh:()=>client.invalidateQueries(),connectPolicy:async()=>{
    state.dbs=[{key:'blank',name:'빈 시험 정책표',local_count:state.localCount}];await client.invalidateQueries();
},completeNews:async()=>{
    state.workflow={...state.workflow,state:'complete',stage:'finished',active:false,can_resume:false,summary:{analysis:{review_required_count:0}}};await client.invalidateQueries();
}};
welfareApi.getStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getConfig=async()=>({is_configured:state.ai,notion_configured:state.dbs.length>0,TARGET_REGION:'',TARGET_AGE_MAX_MONTHS:null});
welfareApi.getAiBacklogStatus=async()=>({backlog_total:0});
welfareApi.getDefaultNewsCondition=async()=>({profile:copy(state.profile),legacy_collection_available:false,initial_settings:{}});
welfareApi.saveDefaultNewsCondition=async(settings,revision)=>{
    calls.saves.push(copy(settings));
    state.profile={...copy(settings),profile_id:'default-news',revision:revision+1,site_ids:state.sites.map(site=>site.id)};
    return {profile:copy(state.profile),legacy_collection_available:false};
};
welfareApi.getSites=async()=>copy(state.sites);
welfareApi.getSitesHealth=async()=>[];
welfareApi.getBatchStatus=async()=>({is_running:false,progress:state.sites.length,total:state.sites.length,success_count:state.sites.length,fail_count:0,review_count:0});
welfareApi.addSitesBatch=async urls=>{
    calls.siteAdds.push(copy(urls));state.sites=urls.map((url,index)=>({id:String(index+1),name:'시험 기관 게시판',url,enabled:true,registration_status:'basic_verified'}));
    if(state.profile)state.profile={...state.profile,revision:state.profile.revision+1,site_ids:state.sites.map(site=>site.id)};
    return {status:'started',count:urls.length};
};
welfareApi.getNewsProfiles=async()=>({items:state.profile?[copy(state.profile)]:[]});
welfareApi.getNewsWorkflows=async()=>({items:state.workflow?[copy(state.workflow)]:[]});
welfareApi.getNewsWorkflow=async()=>copy(state.workflow);
welfareApi.getNewsWorkflowOptions=async()=>({enabled:state.sites.length>0,reason:state.sites.length?'':'연결된 수집 사이트가 없습니다.',reason_code:state.sites.length?'':'SITES_REQUIRED',ai_required:true,analysis_ready:state.ai,analysis_reason:state.ai?'':'AI 연결 후 분석할 수 있습니다. 원문은 먼저 수집할 수 있습니다.',selection:{...copy(state.profile),sites:copy(state.sites)}});
welfareApi.startNewsWorkflow=async(id,revision)=>{calls.starts.push({id,revision});state.workflow={workflow_id:'blank-run',run_id:'blank-run',profile_id:id,revision,state:'running',active:true,stage:'collecting',summary:{}};return copy(state.workflow);};
welfareApi.getNewsFeed=async()=>({items:[],total:0,has_more:false,configured:state.dbs.length>0,counts:{ready:0,review:0,processing:0,records:0}});
welfareApi.getPolicyDbs=async()=>({dbs:state.dbs.map(db=>({...db,local_count:state.localCount}))});
welfareApi.getPolicyChanges=async()=>({items:[]});
welfareApi.getPolicyCandidates=async()=>({items:[]});
welfareApi.getPolicyHealth=async()=>({});
welfareApi.getPolicyReviews=async()=>({items:[],total:0,truncated:false});
for(const key of ['getPolicyStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRecomputeStatus','getPolicyRestyleStatus'])welfareApi[key]=async()=>({running:false});
welfareApi.getPolicyFullCheckStatus=async()=>copy(state.policyStatus);
welfareApi.getPolicyFullCheckPlan=async()=>({policy_count:state.localCount,initial_count:state.localCount,pending_count:0,style_count:0,style_configured:false,ai_remaining:null});
welfareApi.runPolicyFullCheck=async keys=>{calls.checks.push(copy(keys));state.policyStatus={running:true,state:'running',phase:'sync',message:'첫 정책 비교 준비'};return {ok:true};};
welfareApi.previewPolicyDraftFromUrl=async(url,dbKey,title)=>{
    calls.previews.push({url,dbKey,title});if(state.previewFailure)throw {response:{status:422,data:{detail:{error:'POLICY_DRAFT_REVIEW_REQUIRED',message:'원문에서 정책 내용을 확인하지 못했습니다.'}}}};
    return {url,db_key:dbKey,fields:{title:title||'첫 시험 정책',category:'',residence:[],life_cycle:[],target_characteristics:[],start_age_months:null,end_age_months:null,age_detail:'',target_income:'',eligibility_detail:'원문에서 확인한 대상',support_detail:'원문에서 확인한 지원',contact:''},options:{category:[],residence:[],life_cycle:[],target_characteristics:[]},source_review:{required:true,source_complete:false,reasons:['page_char_limit'],source_text:'진단용 원문 일부'}};
};
welfareApi.createPolicyDraftFromUrl=async(...args)=>{calls.creates.push(copy(args));state.localCount++;return {success:true};};
function Fixture(){const navigate=useNavigate();return <main className="mx-auto max-w-5xl p-5"><nav className="mb-4 flex gap-4"><Link to="/news">새 소식 시험</Link><Link to="/policy">정책 시험</Link><Link to="/sites">사이트 시험</Link></nav><p className="text-xs">격리 진단 · 외부 API 차단 · 모의 자료</p><Routes>
    <Route path="/news" element={<NotionUpload/>}/><Route path="/sites" element={<Sites/>}/><Route path="/policy" element={<PolicyManage/>}/>
    <Route path="/settings" element={<DefaultNewsConditionEditor onSaved={()=>navigate('/news')}/>}/>
    </Routes><Toaster/></main>;}
createRoot(document.getElementById('root')).render(<MemoryRouter initialEntries={['/news']}><QueryClientProvider client={client}><ScrapeProvider><Fixture/></ScrapeProvider></QueryClientProvider></MemoryRouter>);

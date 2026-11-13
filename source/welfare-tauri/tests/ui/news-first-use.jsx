import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,Routes,Route,useNavigate,useLocation} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import {queryKeys} from '../../src/api/queryKeys';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import NewsCollectionPanel from '../../src/components/NewsCollectionPanel';
import DefaultNewsConditionEditor from '../../src/components/DefaultNewsConditionEditor';
import {newCollectionCondition} from '../../src/utils/newCollectionCondition';
import '../../src/index.css';

const mode=new URLSearchParams(location.search).get('mode')||'fresh';
const copy=value=>structuredClone(value);
const registered=mode==='changed-before-confirm';
let profile=mode.startsWith('registration')||registered
    ?{...newCollectionCondition(null),profile_id:'default-news',revision:1,name:'시험 기본 조건',site_ids:registered?['1']:[]}:null;
let sites=registered?[{id:'1',name:'시험 기관'}]:[];
let legacyRunning=mode==='legacy-running';
const calls={options:0,starts:[],legacyStarts:0,stops:0,saves:[],unexpected:[]};
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{
    calls.unexpected.push(name);throw Error(`실제 작업 차단: ${name}`);
};
const initial=mode==='preserved'?{region:'부산광역시',min_age_months:0,max_age_months:144,
    ai_prompt:'기존 문구 보존'}:{};
welfareApi.getDefaultNewsCondition=async()=>({profile:copy(profile),
    legacy_collection_available:mode==='legacy',initial_settings:copy(initial)});
welfareApi.getConfig=async()=>({is_configured:mode!=='fresh-no-key',TARGET_REGION:'도봉',TARGET_AGE_MAX_MONTHS:108});
welfareApi.getSites=async()=>copy(sites);
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getAiBacklogStatus=async()=>({backlog_total:0});
welfareApi.getStatus=async()=>({is_running:legacyRunning,progress:30,message:'기존 작업 진행 중'});
welfareApi.startScrape=async()=>{calls.legacyStarts++;return {};};
welfareApi.stopScrape=async()=>{calls.stops++;legacyRunning=false;return {};};
welfareApi.getNewsWorkflows=async()=>({items:[]});
welfareApi.getNewsWorkflowOptions=async()=>{
    calls.options++;
    if(window.holdNextOptions){window.holdNextOptions=false;await new Promise(resolve=>{window.releaseOptions=resolve;});}
    return {enabled:sites.length>0,ai_required:true,analysis_ready:mode!=='fresh-no-key',
        analysis_reason:mode==='fresh-no-key'?'원문은 먼저 수집할 수 있습니다. AI 연결을 확인해주세요.':'',
        reason:sites.length?'':'연결된 수집 사이트가 없습니다.',reason_code:sites.length?'':'SITES_REQUIRED',
        selection:{...copy(profile),sites:copy(sites)}};
};
welfareApi.startNewsWorkflow=async(...args)=>{calls.starts.push(args);return {workflow_id:'fake-started'};};
welfareApi.saveDefaultNewsCondition=async(settings,revision)=>{
    if(revision!==(profile?.revision||0))throw Error('조건 충돌');
    calls.saves.push(copy(settings));
    // Registered source is synthetic; no site registration/backend is exercised.
    sites=[{id:'1',name:'시험 기관'}];
    profile={...copy(settings),profile_id:'default-news',revision:revision+1,site_ids:['1']};
    return {profile:copy(profile),legacy_collection_available:false};
};
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
client.setQueryData(queryKeys.sites,copy(sites));
window.firstUseCalls=()=>copy(calls);
window.completeRegistration=async({parent=false,invalidate=true}={})=>{
    sites=[{id:'1',name:'추가 기관'}];
    profile={...profile,revision:2,site_ids:['1']};
    if(parent)client.setQueryData(['defaultNewsCondition'],{profile:copy(profile),legacy_collection_available:false});
    if(invalidate)await client.invalidateQueries({queryKey:queryKeys.sites});
};
window.createConditionDuringLegacy=()=>{
    profile={...newCollectionCondition(null),profile_id:'default-news',revision:1,name:'새로 저장한 조건'};
    client.setQueryData(['defaultNewsCondition'],{profile:copy(profile),legacy_collection_available:false});
};
function Fixture(){
    const navigate=useNavigate(),location=useLocation();
    return <main className="mx-auto max-w-4xl space-y-4 p-6">
        <p data-testid="route">{location.pathname+location.search}</p>
        <Routes><Route path="/news" element={<NewsCollectionPanel/>}/>
            <Route path="/settings" element={<DefaultNewsConditionEditor onSaved={()=>navigate('/news')}/>}/></Routes>
    </main>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter initialEntries={['/news']}><QueryClientProvider client={client}>
    <ScrapeProvider><Fixture/></ScrapeProvider></QueryClientProvider></MemoryRouter>);

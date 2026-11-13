import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,Routes,Route,Link} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import Settings from '../../src/pages/Settings';
import GuidedSetup from '../../src/components/onboarding/GuidedSetup';
import NewsProfilesPanel from '../../src/components/NewsProfilesPanel';
import {welfareApi} from '../../src/api/welfare';
import {settingsDrafts} from '../../src/utils/settingsDrafts';
import {useSettingsDraft} from '../../src/hooks/useSettingsDraft';
import '../../src/index.css';

// 격리 브라우저 회귀시험에서 메모리 초안과 화면값의 일치 여부만 확인한다.
// 실제 앱 번들에는 포함되지 않으며 값은 이 시험 탭의 메모리에만 존재한다.
globalThis.__fixtureSettingsDrafts=settingsDrafts;

const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
globalThis.__fixtureRefreshQueries = () => client.invalidateQueries();
// Old saved daily-cap values must not reintroduce a limit editor or warning.
let config={revision:1,GOOGLE_API_KEY:'',is_configured:false,AI_DAILY_REQUEST_LIMIT:20,CUSTOM_AI_PROMPT:'기존 지시문',DATA_RETENTION_DAYS:365,
    TARGET_REGION:'도봉',TARGET_AGE_MAX_MONTHS:108,NOTION_DATABASE_ID:'',NOTION_POLICY_DBS:'[]',POLICY_STYLE_GUIDE:'기존 스타일',
    notion_configured:false,notion_token_configured:false};
let pending=null,fail=false,notify=()=>{},writes=0,profileReads=0,taxonomyReads=0,creates=0;
let verificationFails=false;
globalThis.__fixtureFinishSave=()=>pending?.();
globalThis.__fixtureHasPendingSave=()=>Boolean(pending);
localStorage.setItem('welfare-f5-onboarding-step','1');
const profile = id => ({profile_id:id,revision:1,name:`시험 프로필 ${id}`,region:'전국',min_age_months:null,max_age_months:null,
    life_cycles:[],characteristics:[],keywords:['복지'],excluded_keywords:[],site_ids:[],sources:[],ai_prompt:'',semantic_text:'복지 소식',relevance_enabled:true,
    execution_enabled:true});
const profiles={one:profile('one'),two:profile('two'),legacy:{...profile('legacy'),protected:true,execution_enabled:false}};
const conditionsMode=new URLSearchParams(location.search).get('mode')==='conditions';
if(conditionsMode){profiles.one.name='서울특별시 도봉구 · 아동·청소년·청년·중장년·노년 · 한부모·조손·다문화·장애인 가족을 위한 교육과 돌봄 소식';profiles.two.name='공백없는아주긴수집조건이름'.repeat(6);}
const taxonomy={source_url:'https://www.bokjiro.go.kr',life_cycles:['영유아','아동','청소년','노년'],household_situations:['한부모·조손','장애인'],presets:[
    {id:'senior',label:'노년',name:'노년 복지 소식',life_cycles:['노년'],characteristics:[],keywords:['노년','노인'],semantic_text:'노년층 돌봄 지원'},
    {id:'single_parent',label:'한부모·조손',name:'한부모·조손 복지 소식',life_cycles:[],characteristics:['한부모·조손'],keywords:['한부모','조손'],semantic_text:'한부모가족 지원'},
    {id:'disabled',label:'장애인',name:'장애인 복지 소식',life_cycles:[],characteristics:['장애인'],keywords:['장애인'],semantic_text:'장애인 돌봄 지원'},
]};
for(const name of Object.keys(welfareApi)) if(typeof welfareApi[name]==='function') welfareApi[name]=async()=>{throw Error('격리 시험: 외부 호출 차단');};
// Delayed reads and payload observations remain in this synthetic tab only.
let releaseConfigRead;
const configReadGate=new URLSearchParams(location.search).has('holdConfig')
    ?new Promise(resolve=>{releaseConfigRead=resolve;}):Promise.resolve();
globalThis.__fixtureReleaseConfig=()=>releaseConfigRead?.();
globalThis.__fixtureConfigSaves=[];
welfareApi.getConfig=async()=>{await configReadGate;return {...config};};
let defaultNewsProfile=null;
if(conditionsMode)defaultNewsProfile={...profile('default-news'),name:'기본 수집 조건',region:'서울특별시 도봉구',life_cycles:['아동'],site_ids:['1'],min_age_months:0,max_age_months:119};
welfareApi.getDefaultNewsCondition=async()=>({profile:structuredClone(defaultNewsProfile)});
welfareApi.saveDefaultNewsCondition=async(settings,revision)=>{
    if(revision!==(defaultNewsProfile?.revision||0))throw Error('모의 조건 충돌');
    defaultNewsProfile={...structuredClone(settings),profile_id:'default-news',revision:revision+1};
    return {profile:structuredClone(defaultNewsProfile)};
};
globalThis.__fixtureReadDefaultCondition=()=>structuredClone(defaultNewsProfile);
globalThis.__fixtureBumpDefaultRevision=()=>{defaultNewsProfile.revision++;};
globalThis.__fixtureConditionState=()=>({defaultProfile:structuredClone(defaultNewsProfile),profiles:structuredClone(profiles),writes,creates,profileReads,taxonomyReads});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:false,reason_code:'QUALITY_VALIDATION_REQUIRED',reason:'품질 검증이 필요합니다.'});
globalThis.__fixtureReadConfig=()=>structuredClone(config);
welfareApi.getAiStatus=async()=>({busy:false});
window.__fixtureOs={registered:true,reads:0,deletes:0,registers:0,readFailure:false,deleteResult:null,deleteFailure:false};
welfareApi.getSchedule=async()=>({enabled:false,mode:'daily',time:'09:00',interval_hours:6,headless:true});
welfareApi.getPolicyWatchSchedule=async()=>({enabled:false,day_of_week:'mon',time:'09:00'});
welfareApi.getOsTask=async()=>{const state=window.__fixtureOs;state.reads++;return {supported:true,registered:state.readFailure?null:state.registered,...(state.readFailure?{error:'현재 등록 상태를 확인하지 못했습니다.'}:{})};};
welfareApi.deleteOsTask=async()=>{const state=window.__fixtureOs;state.deletes++;if(state.deleteFailure)throw {response:{status:502,data:{detail:{message:'Windows 작업 스케줄러의 등록 상태를 확인하지 못했습니다.'}}}};if(state.deleteResult)return structuredClone(state.deleteResult);state.registered=false;return {status:'success',registered:false};};
welfareApi.registerOsTask=async()=>{window.__fixtureOs.registers++;window.__fixtureOs.registered=true;return {status:'success',registered:true};};
welfareApi.getSites=async()=>[{id:1}];
welfareApi.getNewsProfiles=async()=>{profileReads++;notify();return {items:Object.values(profiles)};};
welfareApi.getNewsProfile=async id=>({profile:structuredClone(profiles[id])});
welfareApi.getNewsProfileTaxonomy=async()=>{taxonomyReads++;notify();return structuredClone(taxonomy);};
welfareApi.createNewsProfile=async settings=>{
    writes++;creates++;
    const id=`new-${creates}`;
    await new Promise((resolve,reject)=>{pending=()=>{pending=null;if(fail){fail=false;reject(Error('모의 생성 실패'));}else{profiles[id]={profile_id:id,revision:1,...structuredClone(settings)};resolve();}notify();};notify();});
    return {profile_id:id,revision:1};
};
welfareApi.cloneNewsProfile=async(id,name)=>{writes++;const clone={...structuredClone(profiles[id]),profile_id:`copy-${id}`,name};profiles[clone.profile_id]=clone;return {profile_id:clone.profile_id};};
welfareApi.saveNewsProfile=async(id,revision,settings)=>{
    writes++;
    await new Promise((resolve,reject)=>{pending=()=>{pending=null;if(fail){fail=false;reject(Error('모의 저장 실패'));}else if(revision!==profiles[id].revision){reject(Error('새 버전 충돌'));}else{profiles[id]={...profiles[id],...settings,revision:revision+1};resolve();}notify();};notify();});
    return {profile_id:id,revision:profiles[id].revision};
};
welfareApi.getPolicyDbs=async()=>({dbs:config.fixture_policy_verified ? [{key:'fixture-policy',verification_ok:true,last_verified_at:'2026-09-04T01:00:00Z',last_sync_at:null,local_count:0}] : []});
welfareApi.getDisconnectedPolicyData=async()=>({items:[],count:0});
welfareApi.getKeywords=async()=>({keywords:[]});
welfareApi.getExcludedKeywords=async()=>({keywords:[]});
globalThis.__fixtureGoogleVerifications = 0;
welfareApi.verifyGoogleApiKey=async()=>{
    globalThis.__fixtureGoogleVerifications++;
    if (globalThis.__fixtureVerificationDelay) await new Promise(resolve => setTimeout(resolve, globalThis.__fixtureVerificationDelay));
    return {ok:!verificationFails,revision:config.revision,error:verificationFails?'모의 연결 실패':undefined};
};
welfareApi.verifyNotion=async()=>({ok:!verificationFails,revision:config.revision,title:'복지F5 새 소식',error:verificationFails?'모의 연결 실패':undefined});
welfareApi.verifyPolicyDbs=async()=>({ok:!verificationFails && JSON.parse(config.NOTION_POLICY_DBS || '[]').length>0,revision:config.revision,items:JSON.parse(config.NOTION_POLICY_DBS || '[]').map(db=>({key:db.key,ok:!verificationFails,title:globalThis.__fixturePolicyTitle ?? db.name,checked_at:'2026-09-04T01:00:00Z',option_counts:{category:4}}))});
welfareApi.saveConfig=async payload=>{writes++;globalThis.__fixtureConfigSaves.push(structuredClone(payload));await new Promise((resolve,reject)=>{pending=()=>{pending=null;if(fail){fail=false;reject(Error('모의 저장 실패'));}else{
    config={...config,...payload,
        ...(Object.hasOwn(payload,'AI_DAILY_REQUEST_LIMIT')?{AI_DAILY_REQUEST_LIMIT:Number(payload.AI_DAILY_REQUEST_LIMIT)}:{}),
        ...(payload.GOOGLE_API_KEY?{is_configured:true}:{}),
        ...(payload.NOTION_TOKEN?{notion_token_configured:true}:{}),
        ...(Object.hasOwn(payload, 'NOTION_DATABASE_ID')?{notion_configured:Boolean(payload.NOTION_DATABASE_ID)}:{}),
        revision:Number(config.revision||0)+1,
    };resolve();}notify();};notify();});return {status:'success',revision:config.revision};};
function Fixture(){
    const [,update]=useState(0);notify=()=>update(n=>n+1);
    const [width,setWidth]=useState(conditionsMode?1200:900);
    const [connectionState,setConnectionState]=useState('none');
    const [probeApiKey]=useSettingsDraft('apiKey','');
    const showConnectionState=state=>{
        const full=state==='full';
        config={...config,
            revision:config.revision+1,
            is_configured:state!=='none',
            notion_token_configured:state==='notion'||full,
            notion_configured:full,
            NOTION_DATABASE_ID:full?'https://www.notion.so/fixture-news':'' ,
            NOTION_POLICY_DBS:full?JSON.stringify([{key:'fixture-policy',name:'복지F5 정책',data_source_id:'https://www.notion.so/fixture-policy'}]):'[]',
            fixture_policy_verified:full,
        };
        setConnectionState(state);
    };
    return <div className="mx-auto p-4" style={{width,maxWidth:'100%'}}><aside className="mb-5 space-y-3 border p-4">
        <p>격리 시험 · 실제 API/운영 설정 접근 없음 · 요청 {writes}회</p>
        <p data-testid="api-key-draft-probe">AI 키 초안 길이 {String(probeApiKey || '').length}자</p>
        <p>조건 목록 조회 {profileReads}회 · 분류 조회 {taxonomyReads}회 · 조건 생성 {creates}회</p>
        <button className="mr-4" onClick={()=>setWidth(582)}>내용 폭 582px</button><button onClick={()=>setWidth(900)}>내용 폭 900px</button>
        <div className="flex flex-wrap gap-2"><span>연결 상태:</span>
            <button onClick={()=>showConnectionState('none')}>모두 미설정</button>
            <button onClick={()=>showConnectionState('gemini')}>Gemini만 완료</button>
            <button onClick={()=>showConnectionState('notion')}>Notion 토큰까지</button>
            <button onClick={()=>showConnectionState('full')}>모두 연결</button>
            <span>현재 {connectionState}</span>
        </div>
        <button className="mr-4" disabled={!pending} onClick={()=>pending?.()}>저장 응답 완료</button>
        <button className="mr-4" onClick={()=>{fail=true;notify();}}>다음 저장 실패</button>
        <button className="mr-4" onClick={()=>{verificationFails=true;notify();}}>연결 확인 실패 설정</button>
        <button className="mr-4" onClick={()=>{verificationFails=false;notify();}}>연결 확인 정상 설정</button>
        <Link to="/elsewhere">다른 화면</Link>
        <Link className="ml-4" to="/settings?tab=general">예전 기본 설정 주소</Link>
        <Link className="ml-4" to="/settings?tab=notion">예전 Notion 설정 주소</Link>
        <Link className="ml-4" to="/guide">도우미 시험</Link><Link className="ml-4" to="/profiles">프로필 시험</Link>
        <Link className="ml-4" to="/settings?tab=guide&step=google">AI 단계 시험</Link>
        <Link className="ml-4" to="/settings?tab=guide&step=target">수집 조건 단계 시험</Link>
        <Link className="ml-4" to="/settings?tab=guide&step=policy">정책 단계 시험</Link>
        <Link className="ml-4" to="/settings?tab=guide&step=news">보관함 단계 시험</Link>
        <button className="ml-4" onClick={()=>{profiles.one={...profiles.one,revision:profiles.one.revision+1};notify();}}>외부에서 프로필 갱신</button>
    </aside><Routes><Route path="/settings" element={<Settings key={connectionState}/>}/><Route path="/guide" element={<GuidedSetup/>}/><Route path="/profiles" element={<NewsProfilesPanel/>}/><Route path="*" element={<Link to="/settings?tab=connections">설정으로 돌아가기</Link>}/></Routes><Toaster/></div>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter initialEntries={['/settings?tab=connections']}><QueryClientProvider client={client}><Fixture/></QueryClientProvider></MemoryRouter>);

// Production layout/pages; synthetic memory-only responses from the existing UI fixtures.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {ThemeProvider} from '../../src/context/ThemeContext';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

window.a11yFixture={unexpected:[],writes:[],reads:[]};
const state=window.a11yFixture;
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{
    state.unexpected.push(name);throw Error(`Synthetic fixture blocked API: ${name}`);
};
const read=(name,result)=>{welfareApi[name]=async()=>{state.reads.push(name);return structuredClone(result);};};
const site={id:1,name:'시험 복지 지원 기관',url:'https://example.invalid/board',list_selector:'.test',title_selector:'.title',link_selector:'a'};
const news={id:1,title:'시험 복지 지원 소식 — 대상과 신청 방법을 확인하는 긴 제목',org:'시험 가족문화지원기관',site_name:'시험 기관',status:'진행 예정',category:'생활지원',support_detail:'합성 자료의 지원 내용입니다.',link:'https://example.invalid/news',start:'2026-09-15',end:'2026-09-20'};
const change={id:1,title:'시험 정책 1',intensity:'high',fields:[{field:'추가 자격요건',old:'기존 신청 조건',new:'새 신청 조건',level:'high',requires_individual_review:true}]};
read('getStatus',{is_running:false,progress:0});read('getAiStatus',{busy:false});
read('getConfig',{revision:'fixture',is_configured:true,GOOGLE_API_KEY:'',TARGET_REGION:'도봉구',TARGET_AGE_MAX_MONTHS:108,NOTION_POLICY_DBS:'[]',notion_configured:true,notion_token_configured:true,DATA_RETENTION_DAYS:365});
read('verifyGoogleApiKey',{ok:true,revision:'fixture'});read('getSites',[site]);
read('getBatchStatus',{is_running:false});read('getDefaultNewsCondition',{profile:null});
read('getNewsProfiles',{items:[]});read('getNewsWorkflows',{items:[]});
read('getNewsWorkflowOptions',{enabled:false,reason_code:'QUALITY_VALIDATION_REQUIRED',reason:'합성 수집 조건입니다.'});
read('getAiBacklogStatus',{backlog_total:0});read('getSummaryStats',{total:1,by_category:{생활지원:1},by_site:{'시험 기관':1}});
read('getTrends',[{day:'2026-09-08',count:1}]);read('searchItems',{items:[news],total:1});read('semanticSearch',{items:[news],total:1});
read('getChangedItems',{items:[]});read('getExpiringItems',{total:1,items:[{id:1,title:'시험 마감 소식',deadline:'2026-09-14',link:'https://example.invalid/event'}]});
read('getNotionCandidates',{configured:true,count:2,items:[news,{...news,id:2,title:'공백없는아주긴복지소식제목'.repeat(7)}]});
read('getPolicyDbs',{dbs:[{key:'test',name:'시험 정책표'}]});read('getPolicyChanges',{items:[change]});
read('getPolicyCandidates',{items:[]});read('getPolicyHealth',{items:[],total:0});
for(const name of ['getPolicyFullCheckStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRestyleStatus','getPolicyRecomputeStatus','getPolicyStatus'])read(name,{running:false});
read('getPolicyTaskStatuses',['정책 최신화','Notion 불러오기','정책 비교','스타일 제안 생성','변경 제안 다시 확인','출처 변경 확인'].map(label=>({label,running:false})));
read('getDisconnectedPolicyData',{items:[],count:0});read('getSitesHealth',[{site_id:1,site_name:site.name,status:'ok',total_runs_30d:1,total_items_found:1,success_rate:100}]);
read('getHealth',{sites:[{site_name:site.name,status:'ok'}]});read('getBokjiroStatus',{is_running:false});read('getLogs',[]);read('getLogFiles',[]);
read('getKeywords',{keywords:[]});read('getExcludedKeywords',{keywords:[]});
read('getSchedule',{enabled:false,mode:'daily',time:'09:00',interval_hours:6,headless:true});
read('getPolicyWatchSchedule',{enabled:false,day_of_week:'mon',time:'09:00'});read('getOsTask',{supported:true,registered:false});
// No write endpoint is made available, including config, download, verification
// mutations, collection, Notion registration, policy application and AI calls.
localStorage.setItem('welfare-f5-welcome-seen','true');
localStorage.setItem('welfare-f5-home-setup-card-collapsed','true');
localStorage.setItem('welfare-f5-onboarding-step','0');
localStorage.setItem('sidebar-collapsed','false');
const query=new URLSearchParams(location.search), kind=query.get('page')||'home';
const imports={home:()=>import('../../src/pages/Home'),reports:()=>import('../../src/pages/Reports'),sites:()=>import('../../src/pages/Sites'),search:()=>import('../../src/pages/Search'),system:()=>import('../../src/pages/Settings'),guide:()=>import('../../src/pages/Settings'),news:()=>import('../../src/pages/NotionUpload'),policy:()=>import('../../src/pages/PolicyManage')};
// Layout captures polling methods at import: install mocks first.
const {default:Layout}=await import('../../src/components/layout/Layout');
const {default:Page}=await imports[kind]();
const route=kind==='system'?'/settings?tab=system':kind==='guide'?'/settings?tab=guide':kind==='home'?'/':kind==='news'?'/notion':`/${kind}`;
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
createRoot(document.getElementById('root')).render(<ThemeProvider><MemoryRouter initialEntries={[route]}><QueryClientProvider client={client}><ScrapeProvider><Layout><Page/></Layout></ScrapeProvider></QueryClientProvider></MemoryRouter></ThemeProvider>);

import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

// Production pages with memory-only responses. Never read config or use live APIs.
window.unexpectedCalls=[];
for(const key of Object.keys(welfareApi)) if(typeof welfareApi[key]==='function') welfareApi[key]=async()=>{window.unexpectedCalls.push(key);throw Error(`Blocked API: ${key}`);};
welfareApi.getSites=async()=>[{id:'fixture',name:'시험 복지 지원 기관',url:'https://example.invalid/board',list_selector:'.test',title_selector:'.title',link_selector:'a'}];
welfareApi.getBatchStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getSitesHealth=async()=>[{site_id:'fixture',site_name:'시험 복지 지원 기관',status:'warning',total_runs_30d:3,total_items_found:2,success_rate:67}];
welfareApi.getSummaryStats=async()=>({total:1,by_category:{생활지원:1},by_site:{'시험 기관':1}});
welfareApi.getTrends=async()=>[{day:'2026-09-08',count:1}];
const search=async()=>({items:[{id:'fixture',title:'시험 복지 지원 소식',category:'생활지원',site_name:'시험 기관',support_detail:'시험 상세 내용입니다.',link:'https://example.invalid/news'}],total:1});
welfareApi.searchItems=search;
welfareApi.semanticSearch=search;
const pages={reports:()=>import('../../src/pages/Reports'),sites:()=>import('../../src/pages/Sites'),search:()=>import('../../src/pages/Search')};
const kind=new URLSearchParams(location.search).get('page')||'reports';
const {default:Page}=await pages[kind]();
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><style>{'@media (min-width:768px) { #page-hierarchy-shell { padding-left:260px; } }'}</style><div id="page-hierarchy-shell"><main className="mx-auto min-h-screen max-w-7xl bg-gray-100 p-4 text-gray-800 md:p-8"><Page/></main></div></QueryClientProvider></MemoryRouter>);

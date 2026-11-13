import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import {welfareApi} from '../../src/api/welfare';
import {queryKeys} from '../../src/api/queryKeys';
import '../../src/index.css';

const kind=new URLSearchParams(location.search).get('page')||'sites';
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
const fixture=window.functionFixture={client,queryKeys,fail:{sites:false,stats:false,trends:false,health:false,history:false},calls:[],unexpected:[],pending:[],health:[],history:[],
 sites:[{id:'fixture',name:'시험 복지 기관',url:'https://example.invalid/board',selectors:{list:'.item'}}],
 stats:{total:1,by_category:{생활지원:1},by_site:{'시험 기관':1}},trends:[{day:'2026-09-08',count:1}]};
for(const key of Object.keys(welfareApi)) if(typeof welfareApi[key]==='function') welfareApi[key]=async()=>{fixture.unexpected.push(key);throw Error('Blocked API: '+key);};
for(const [name,key] of [['getSites','sites'],['getSummaryStats','stats'],['getTrends','trends']]) welfareApi[name]=async()=>{
 fixture.calls.push({name});if(fixture.fail[key])throw Error('Synthetic unavailable');return fixture[key];
};
welfareApi.getBatchStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getSitesHealth=async()=>{if(fixture.fail.health)throw Error('Synthetic unavailable');return fixture.health;};
welfareApi.getSiteHealth=async()=>{if(fixture.fail.history)throw Error('Synthetic unavailable');return fixture.history;};
for(const name of ['addSitesQuick','addSitesBatch']) welfareApi[name]=async urls=>{
 fixture.calls.push({name,urls});return await new Promise((resolve,reject)=>fixture.pending.push({resolve,reject}));
};
if(new URLSearchParams(location.search).has('fail')) fixture.fail={sites:true,stats:true,trends:true};
const {default:Page}=await (kind==='reports'?import('../../src/pages/Reports'):import('../../src/pages/Sites'));
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><main className="mx-auto max-w-6xl bg-gray-100 p-5 text-gray-800 dark:bg-gray-950 dark:text-gray-100"><Page/></main><Toaster/></QueryClientProvider></MemoryRouter>);

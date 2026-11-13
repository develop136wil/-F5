import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

// 실제 감시 페이지만 렌더링하며 네트워크·삭제·알림·AI는 모두 메모리 모의 구현이다.
const mode=new URLSearchParams(location.search).get('mode');
const makeItem=id=>({id,name:`시험 정책 ${id}`,url:`https://example.org/policy/${id}`,scope:'일반',enabled:true,last_checked:'2026-09-08T01:00:00Z',last_changed_at:'2026-09-08T01:00:00Z',change_count:1,prev_text:`이전 안내 ${id}`,last_text:`현재 안내 ${id}`});
const state=window.watchFixture={items:mode==='seed'?[makeItem(1),makeItem(2)]:[],calls:{list:0,add:[],check:0,diff:[],toggle:[],delete:[]},failures:{},holds:{},unexpected:[]};
let nextId=3;
const pending={};
window.finishWatch=async key=>{const resolve=pending[key]?.shift();if(!resolve)throw Error(`대기 요청 없음: ${key}`);resolve();};
async function step(key){if(state.holds[key])await new Promise(resolve=>(pending[key]||=[]).push(resolve));if(state.failures[key])throw state.failures[key];}
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{state.unexpected.push(name);throw Error(`실제 호출 차단: ${name}`);};
welfareApi.getPolicyWatch=async()=>{state.calls.list++;await step('list');return structuredClone(state.items);};
welfareApi.addPolicyWatch=async value=>{state.calls.add.push(structuredClone(value));await step('add');const item={id:nextId++,name:value.name||value.url,url:value.url,scope:value.scope,enabled:true,last_checked:null,last_changed_at:null,change_count:0};state.items.push(item);return {id:item.id};};
welfareApi.runPolicyWatchCheck=async()=>{state.calls.check++;await step('check');const active=state.items.filter(item=>item.enabled);for(const item of active)Object.assign(item,{last_checked:'2026-09-08T02:00:00Z',last_changed_at:'2026-09-08T02:00:00Z',change_count:item.change_count+1,prev_text:'지원 인원 10명',last_text:'지원 인원 20명'});return {checked:active.length,changed:active.length,failed:0,status:'completed'};};
welfareApi.getPolicyWatchDiff=async id=>{state.calls.diff.push(id);await step(`diff-${id}`);return structuredClone(state.items.find(item=>item.id===id));};
welfareApi.togglePolicyWatch=async(id,enabled)=>{state.calls.toggle.push({id,enabled});await step('toggle');state.items.find(item=>item.id===id).enabled=enabled;return {status:'success'};};
welfareApi.deletePolicyWatch=async id=>{state.calls.delete.push(id);await step('delete');state.items=state.items.filter(item=>item.id!==id);return {status:'success'};};
const {default:PolicyWatch}=await import('../../src/pages/PolicyWatch');
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
createRoot(document.getElementById('root')).render(<QueryClientProvider client={client}><main className="mx-auto max-w-6xl bg-gray-100 p-4 text-gray-800 dark:bg-gray-900 dark:text-gray-100"><PolicyWatch/></main><Toaster/></QueryClientProvider>);

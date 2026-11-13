import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Routes, Route, Link} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import PolicyManage from '../../src/pages/PolicyManage';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

const mode = new URLSearchParams(location.search).get('mode');
window.fullCalls = [];
window.fullStatus = mode === 'resume'
    ? {running:true,phase:'style',state:'running',message:'통합 문장 정리 중',checked:2,total:6}
    : {running:false,state:'idle'};
const partialStatuses = {
    'embedding-failed': {initial_compared:1,changed:0,ai_required:1,ai_completed:1,ai_failed:0,
        embedding_completed:0,embedding_failed:1,style_failed:0,style_rejected_fields:0,
        review_count:0,review_notes:[],message:'제목 검색 데이터 준비 실패 1건. 잠시 후 정책 정보 최신인지 확인하기로 재시도하세요.'},
    'analysis-failed': {initial_compared:0,changed:0,ai_required:1,ai_completed:0,ai_failed:1,
        embedding_completed:1,embedding_failed:0,style_failed:0,style_rejected_fields:0,
        review_count:0,review_notes:[],message:'AI 분석 미완료 1건. 잠시 후 정책 정보 최신인지 확인하기로 재시도하세요.'},
    'review-held': {review_count:1,review_notes:[{title:'검토 정책',code:'source_read_incomplete',note:'원문 확인 필요'}],
        message:'아래 확인이 필요한 정책에 표시된 사유를 먼저 확인해주세요.'},
};
if (partialStatuses[mode]) window.fullStatus = {running:false,state:'partial',phase:'done',...partialStatuses[mode]};
window.fullPlan = {policy_count:3,initial_count:2,pending_count:1,style_count:3,style_configured:true,ai_remaining:0};
if (mode === 'noai') Object.assign(window.fullPlan,{initial_count:0,style_count:0});
if (mode === 'empty') Object.assign(window.fullPlan,{policy_count:0,initial_count:0,pending_count:0,style_count:0,style_configured:false});
if (mode === 'invalid') delete window.fullPlan.initial_count;
for (const key of Object.keys(welfareApi)) if (typeof welfareApi[key] === 'function') {
    welfareApi[key] = async () => {window.fullCalls.push({key}); throw Error(`실제 호출 금지: ${key}`);};
}
const mockRead = (key, value) => {welfareApi[key] = async () => {window.fullCalls.push({key}); return structuredClone(value);};};
mockRead('getAiStatus',{busy:false});
mockRead('getPolicyDbs',{dbs:[{key:'fixture',name:'격리 시험 정책표'}]});
mockRead('getPolicyChanges',{items:[]});
mockRead('getPolicyCandidates',{items:[]});
mockRead('getPolicyHealth',{});
mockRead('getPolicyReviews',{items:[],total:0,truncated:false});
for (const key of ['getPolicyStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRecomputeStatus','getPolicyRestyleStatus']) {
    welfareApi[key] = async () => {
        window.fullCalls.push({key});
        return {running:mode === 'resume' && window.fullStatus.running,message:`중복 하위 단계 ${key}`};
    };
}
welfareApi.getPolicyFullCheckStatus = async () => {
    window.fullCalls.push({key:'getPolicyFullCheckStatus'});
    return structuredClone(window.fullStatus);
};
welfareApi.getPolicyFullCheckPlan = async keys => {
    window.fullCalls.push({key:'getPolicyFullCheckPlan',keys});
    if (mode === 'error') throw Error('대상 조회 시험 오류');
    return structuredClone(window.fullPlan);
};
welfareApi.runPolicyFullCheck = async keys => {
    window.fullCalls.push({key:'runPolicyFullCheck',keys});
    window.fullStatus = {running:true,phase:'sync',state:'running',message:'통합 불러오기 중',checked:1,total:3};
    return {ok:true};
};
createRoot(document.getElementById('root')).render(
    <MemoryRouter initialEntries={['/policy']}><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>
        <div className="flex h-screen min-w-0 bg-[#F2F4F6]">
            <nav aria-label="격리 시험 이동" className="w-16 shrink-0 bg-white p-2"><Link to="/away">다른 화면</Link><br/><Link to="/policy">정책 화면</Link></nav>
            <main className="min-w-0 flex-1 overflow-auto p-4 sm:p-8"><Routes><Route path="/policy" element={<PolicyManage/>}/><Route path="/away" element={<h1>다른 시험 화면</h1>}/></Routes></main>
        </div><Toaster/>
    </QueryClientProvider></MemoryRouter>,
);

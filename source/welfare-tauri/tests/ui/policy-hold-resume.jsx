import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Routes, Route, Link} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import PolicyManage from '../../src/pages/PolicyManage';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

const mode = new URLSearchParams(location.search).get('mode') || 'check';
window.fixtureCalls = [];
for (const key of Object.keys(welfareApi)) if (typeof welfareApi[key] === 'function') {
    welfareApi[key] = async () => {window.fixtureCalls.push(key); throw Error(`실제 호출 금지: ${key}`);};
}
const mockRead = (key, value) => {welfareApi[key] = async () => {window.fixtureCalls.push(key); return structuredClone(value);};};
mockRead('getAiStatus', {busy:false});
mockRead('getPolicyDbs', {dbs:[{key:'fixture', name:'격리 시험 정책표'}]});
mockRead('getPolicyChanges', {items:[]});
mockRead('getPolicyCandidates', {items:[]});
mockRead('getPolicyHealth', {});
mockRead('getPolicyReviews', {items:[],total:0,truncated:false});
const statusNames = ['getPolicyStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRecomputeStatus','getPolicyFullCheckStatus','getPolicyRestyleStatus'];
statusNames.forEach(name => mockRead(name, {running:false}));
const hold = {
    running:false, message:'점검 종료 · 원문 확인 필요', review_count:3,
    ai_required:0, ai_completed:0, ai_failed:0, ai_preflight_held:3, changed:0,
    review_notes:[
        {title:'청년 지원 정책 · 출생일과 나이 기준', code:'source_criteria_review_required', note:'소득·나이 적용 기준 확인 필요'},
        {title:'여러 정책이 포함된 긴 원문', code:'source_too_long', note:'정책 원문이 분석 범위를 초과했습니다.'},
        {title:'공개 여부를 확인할 자료', code:'ai_input_review_required', note:'민감정보 전송 여부 확인 필요'},
    ],
};
const target = ['full','mirrored'].includes(mode) ? 'getPolicyFullCheckStatus' : mode === 'audit' ? 'getPolicyAuditStatus' : 'getPolicyStatus';
mockRead(target, mode === 'clean' ? {running:false, message:'변경 없음', review_count:0} : hold);
if(mode === 'mirrored') mockRead('getPolicyStatus', hold);
createRoot(document.getElementById('root')).render(
    <MemoryRouter initialEntries={['/policy']}><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>
        <div className="flex h-screen min-w-0 bg-[#F2F4F6] dark:bg-gray-900">
            <nav aria-label="격리 시험 이동" className="w-16 shrink-0 p-2 bg-white dark:bg-gray-900"><Link to="/away">다른 화면</Link><br/><Link to="/policy">정책 화면</Link></nav>
            <main className="flex-1 min-w-0 overflow-auto p-4 sm:p-8 dark:bg-gray-900 dark:text-gray-100"><Routes>
                <Route path="/policy" element={<PolicyManage/>}/><Route path="/away" element={<h1>격리 시험의 다른 화면</h1>}/>
            </Routes></main>
        </div>
    </QueryClientProvider></MemoryRouter>,
);

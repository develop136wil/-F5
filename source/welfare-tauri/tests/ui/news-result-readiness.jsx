import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import NewsProfileResults from '../../src/components/NewsProfileResults';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

window.fixtureCalls=[];
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{throw Error(`실제 호출 금지: ${key}`);};
const base={state:'complete',revision:1,link:'https://example.org/public/news',details:{},source_review:{status:'complete'}};
const items=[
    {...base,record_id:'provider',job_id:'provider',title:'제공기관 모집',target_status:'review',import_eligibility:{allowed:false,reason:'relevance_unconfirmed'},
        review_evidence:{version:1,omitted_count:0,items:[{label:'확보한 원문 · 요약 아님',start_line:1,end_line:3,text:'신청방법: 온라인 신청\n제외: 동일 사업 중복 신청 불가\n<script>window.untrustedExecuted = true</script>'}]}},
    {...base,record_id:'partial',job_id:'partial',title:'첨부 확인이 필요한 행사',target_status:'match',source_review:{status:'partial'},input_note:'첨부파일의 신청 조건은 확인이 필요합니다.',import_eligibility:{allowed:true,reason:'confirmation_required'}},
    {...base,record_id:'empty',job_id:'empty',title:'본문 미확보 자료',target_status:'review',source_review:{status:'empty'},import_eligibility:{allowed:false,reason:'invalid_saved_result'}},
    {...base,record_id:'long',job_id:'long',title:'긴 원문 확인 자료',target_status:'review',import_eligibility:{allowed:false,reason:'relevance_unconfirmed'},review_evidence:{version:1,items:[],omitted_count:1}},
];
welfareApi.getNewsProfileResults=async()=>{window.fixtureCalls.push('getNewsProfileResults');return {items:structuredClone(items),has_more:false};};
welfareApi.previewNewsResultImport=async()=>{window.fixtureCalls.push('previewNewsResultImport');return {status:'ready',title:'첨부 확인이 필요한 행사',revision:1,fingerprint:'a'.repeat(64),note:'첨부파일 원문 확인 필요'};};
welfareApi.importNewsResult=async()=>{window.fixtureCalls.push('importNewsResult');return {status:'created'};};
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><main className="min-h-screen bg-[#F2F4F6] p-4 dark:bg-gray-900"><NewsProfileResults profileId="fixture" onClose={()=>{}}/></main></QueryClientProvider></MemoryRouter>);

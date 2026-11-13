// Manual screenshots of production UI, illustrative data only.
// All real API methods are blocked. Saving/uploading is intentionally unavailable.
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import {welfareApi} from '../../src/api/welfare';
import NotionUpload from '../../src/pages/NotionUpload';
import '../../src/index.css';

const calls=globalThis.newsManualCalls={blocked:[],details:[]};
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{
    calls.blocked.push(key);throw Error('매뉴얼 예시: 실제 호출 금지 '+key);
};
const profile={profile_id:'default-news',name:'현재 수집 조건',revision:1,region:'서울특별시 도봉구',life_cycles:['영유아','아동'],characteristics:[],min_age_months:0,max_age_months:108,publication_period:'30'};
const titles=['[예시] 부모·자녀 함께하는 책놀이 프로그램 모집','[예시] 어린이 문화체험 참여 가족 모집','[예시] 방과 후 아동 돌봄 프로그램 안내'];
const rows=titles.map((title,i)=>({id:'job:manual-'+(i+1),record_id:'job:manual-'+(i+1),job_id:'manual-'+(i+1),
    profile_id:'default-news',fingerprint:String(i+1).padStart(64,'0'),title,
    org:['예시 가족지원기관','예시 어린이문화기관','예시 아동지원기관'][i],
    link:'https://example.org/manual-news/'+(i+1),published_date:'2026-09-18',
    status:i===2?'진행 중':'진행 예정',start:i===0?'2026-09-28':i===1?'2026-10-03':'2026-09-01',end:i===2?'2026-10-30':null,
    selectable:true,view:'ready',state:'complete',upload_uncertain:false,review_required:i===2,
    attachment_review_required:i===2,attachment_note:i===2?'첨부 안내문의 준비물과 신청 조건을 원문에서 확인해주세요.':''}));
welfareApi.getStatus=async()=>({is_running:false});
welfareApi.getAiStatus=async()=>({busy:false});
welfareApi.getDefaultNewsCondition=async()=>({profile});
welfareApi.getNewsProfiles=async()=>({items:[]});
welfareApi.getNewsWorkflowOptions=async()=>({enabled:true,selection:profile});
welfareApi.getNewsWorkflows=async()=>({items:[]});
welfareApi.getNewsFeed=async({view='ready',offset=0}={})=>({items:view==='ready'?rows.slice(offset,offset+10):[],
    total:view==='ready'?rows.length:0,count:view==='ready'?rows.length:0,has_more:false,configured:true,offset,limit:10,
    counts:{ready:rows.length,review:0,processing:0,records:0}});
welfareApi.getNewsResultDetails=async(profileId,recordId)=>{
    calls.details.push({profileId,recordId});
    return {record_id:recordId,ai_called:false,details:{category:'교육·문화',target:'도봉구 거주 아동과 보호자 (설명용 예시)',
        support_detail:'**함께하는 책놀이 프로그램**\n- 그림책을 읽고 이야기 나누기\n- 보호자와 함께하는 만들기 활동\n- 활동 일시: 2026년 9월 28일 오후 2시\n- 활동 장소: 예시 가족지원기관 프로그램실',
        eligibility_detail:'아동과 보호자가 함께 참여합니다.\n기관 홈페이지의 신청 안내에서 참여 조건을 확인한 뒤 신청합니다.\n※ 매뉴얼 설명용 내용이며 실제 모집 공고가 아닙니다.',
        contact:'예시 기관 홈페이지의 문의처 확인',deadline:'2026년 9월 24일까지 (예시)'}};
};
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}})}>
    <ScrapeProvider><main className="mx-auto max-w-6xl p-8"><NotionUpload/></main></ScrapeProvider>
</QueryClientProvider></MemoryRouter>);

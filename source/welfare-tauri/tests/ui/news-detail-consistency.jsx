import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import ItemDetailModal from '../../src/components/ItemDetailModal';
import NewsFeedDetailDialog from '../../src/components/NewsFeedDetailDialog';
import '../../src/index.css';

const mode=new URLSearchParams(location.search).get('mode') || 'normal';
const calls=globalThis.__detailCalls={blocked:[],saved:0,feed:0,similar:0};
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{
    calls.blocked.push(key);throw Error('격리 검사: 실제 API 차단 '+key);
};
const title='[예시] 9월 부모·자녀 체험활동 안내';
const raw='표시되면 안 되는 원문 메뉴 COPYRIGHT <script>window.__unsafe=true</script>';
const support=mode==='boundary'?'### 지원 내용\n지원 내용: 1인당 5만원(총 50,000원)을 지원합니다.\n\n### 기간\n신청 기간: 2026-10-01부터 2026-10-31까지입니다.':mode==='empty'?'':'### 함께하는 놀이 활동\n- 아동과 보호자가 **함께 참여**하는 프로그램입니다.\n- 신청: 기관 홈페이지\n\n'+
    (mode==='long'?Array.from({length:70},(_,i)=>`- 안내 ${i+1}: 원문에서 확인한 예시 활동과 신청 방법입니다.`).join('\n'):'- 문의: 02-0000-0000 (예시)');
const projection={support_detail:support,presentation_kind:mode==='boundary'?'structured':'source_excerpt',source_note:mode==='empty'?'읽을 수 있는 본문이 없습니다. 원문을 확인해주세요.':''};
welfareApi.getItemDetail=async()=>{calls.saved++;return {id:1,title,category:'문화/여가',site_name:'example.invalid',
    target:'아동과 보호자',support_detail:raw,ai_data:{support_detail:raw},link:'https://example.invalid/notice',news_presentation:projection};};
welfareApi.getSimilarItems=async()=>{calls.similar++;return [];};
welfareApi.getNewsResultDetails=async(_profile,record)=>{
    calls.feed++;
    if(mode==='error')throw Error('격리 검사: 상세 조회 실패');
    return {record_id:record,ai_called:false,details:{category:'문화/여가',target:'아동과 보호자',support_detail:support},
        presentation_kind:projection.presentation_kind,source_note:projection.source_note,source_text:raw};
};
function Fixture(){
    const [detail,setDetail]=useState(null);
    return <main className="mx-auto max-w-4xl space-y-5 p-5">
        <p className="text-sm text-gray-500">격리 검사 · 설명용 예시 · 실제 AI·Notion·DB 연결 없음</p>
        <button className="m-2 rounded border px-4 py-2" onClick={()=>setDetail('saved')}>저장 소식 상세</button>
        <button className="m-2 rounded border px-4 py-2" onClick={()=>setDetail('feed')}>새 소식 상세</button>
        {detail==='saved'&&<ItemDetailModal itemId={1} onClose={()=>setDetail(null)} tone="news"/>}
        {detail==='feed'&&<NewsFeedDetailDialog item={{record_id:'job:1',profile_id:'test',state:'complete',
            title,site_name:'example.invalid',link:'https://example.invalid/notice'}} onClose={()=>setDetail(null)}/>}
    </main>;
}
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}})}><Fixture/></QueryClientProvider>);

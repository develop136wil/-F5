import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,useLocation} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import NewsProcessingRecords from '../../src/components/NewsProcessingRecords';
import '../../src/index.css';

const mode=new URLSearchParams(location.search).get('mode')||'legacy';
globalThis.__calls={feed:0,unexpected:[]};
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{
    globalThis.__calls.unexpected.push(key);throw Error('격리 시험: 실제 API 차단');
};
welfareApi.getNewsFeed=async()=>{
    globalThis.__calls.feed++;
    return {total:1,items:[{id:'item:1',item_id:1,title:'이전 연결 실패',state:'failed',
        status:'분석 미완료',workflow_id:mode==='workflow'?'workflow-1':null,
        review_reason:'authentication_required',recovery_action:mode==='workflow'?null:'new_collection',
        input_note:mode==='workflow'?'위에서 작업 재개 여부를 확인해주세요.':'이 이전 기록에는 바로 재개할 연결 작업이 없습니다. 수집 조건을 저장한 뒤 새 소식 찾기를 직접 실행해주세요.'}]};
};
function Fixture(){
    const current=useLocation();
    return <main className="max-w-3xl space-y-5 p-5"><h1>이전 분석 복구 안내</h1>
        <div id="news-collection" tabIndex={-1}>수집 진행</div>
        <NewsProcessingRecords profileId="default-news" counts={{processing:1,review:0,records:0}} onDetail={()=>{}}/>
        <output aria-label="이동 경로">{current.pathname+current.search}</output></main>;
}
const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><Fixture/></QueryClientProvider></MemoryRouter>);

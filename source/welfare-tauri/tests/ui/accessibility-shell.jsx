import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter,Link,useLocation} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MotionConfig,AnimatePresence} from 'framer-motion';
import {ThemeProvider,useTheme} from '../../src/context/ThemeContext';
import {ScrapeProvider} from '../../src/context/ScrapeContext';
import PageTransition from '../../src/components/layout/PageTransition';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

const client=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}});
window.shellFixture={unexpected:[],reads:[]};
let ai={busy:false};
for(const key of Object.keys(welfareApi))if(typeof welfareApi[key]==='function')welfareApi[key]=async()=>{shellFixture.unexpected.push(key);throw Error('Synthetic fixture blocks real API');};
const mock=(key,result)=>{welfareApi[key]=async()=>{shellFixture.reads.push(key);return typeof result==='function'?result():result;};};
mock('getStatus',{is_running:false,progress:0});mock('getAiStatus',()=>ai);mock('getNewsProfiles',{items:[]});
for(const key of ['getPolicyFullCheckStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRestyleStatus','getPolicyRecomputeStatus','getPolicyStatus'])mock(key,{running:false});
mock('getPolicyTaskStatuses',['정책 최신화','Notion 불러오기','정책 비교','스타일 제안 생성','변경 제안 다시 확인','출처 변경 확인'].map(label=>({label,running:false})));
window.setShellBusy=()=>{ai={busy:true,job_label:'시험 분석'};client.setQueryData(['ai-global-status'],ai);};
// The real policy status module captures polling functions during import.
const {default:Layout}=await import('../../src/components/layout/Layout');
function Screen(){
    const location=useLocation(),{darkMode,toggleDarkMode}=useTheme();
    return <Layout><AnimatePresence mode="wait"><PageTransition key={location.pathname}>
        <section data-shell-page={location.pathname} className="space-y-5">
            <h1 className="ui-page-title">{location.pathname==='/'?'시험 홈':`시험 화면 ${location.pathname}`}</h1>
            <p className="ui-description">화면 틀과 키보드 이동만 검사하며 실제 기능은 호출하지 않습니다.</p>
            <button type="button" onClick={toggleDarkMode} aria-pressed={darkMode} className="ui-field">시험 테마 전환</button>
            <output aria-label="시험 테마 상태">{darkMode?'dark':'light'}</output>
            <Link to={location.pathname==='/'?'/search':'/'} className="block py-3 underline">시험 페이지 이동</Link>
        </section>
    </PageTransition></AnimatePresence></Layout>;
}
createRoot(document.getElementById('root')).render(<MotionConfig reducedMotion="user"><QueryClientProvider client={client}><ThemeProvider><ScrapeProvider><MemoryRouter><Screen/></MemoryRouter></ScrapeProvider></ThemeProvider></QueryClientProvider></MotionConfig>);

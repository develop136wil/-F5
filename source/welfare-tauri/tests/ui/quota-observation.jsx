import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import AiQuotaObservation from '../../src/components/AiQuotaObservation';
import {welfareApi} from '../../src/api/welfare';
import {settingsDrafts} from '../../src/utils/settingsDrafts';
import '../../src/index.css';
const mode=new URLSearchParams(location.search).get('mode');
const current={state:'current',observed_on:'2026-09-09',valid_days:7,observed:{'gemini-3.5-flash-lite':{rpm:11,tpm:12345,rpd:321},'gemini-embedding-001':{rpm:12,tpm:23456,rpd:432}},effective:{'gemini-3.5-flash-lite':{rpm:7,tpm:7407,rpd:null},'gemini-embedding-001':{rpm:8,tpm:14073,rpd:null}},input_tokens_are_estimates:true};
let saved=mode==='current'?current:{state:mode==='expired'?'reconfirmation_required':'unconfirmed'};
window.calls=[];window.unexpected=[];window.drafts=settingsDrafts;
for(const name of Object.keys(welfareApi))if(typeof welfareApi[name]==='function')welfareApi[name]=async()=>{window.unexpected.push(name);throw Error('실제 API 차단');};
welfareApi.saveQuotaObservation=async payload=>{
    window.calls.push(payload);
    await new Promise((resolve,reject)=>{window.finishQuota=()=>{saved={...current,observed:payload.models};resolve();};window.failQuota=()=>reject(Error('모의 한도 저장 실패'));});
    return {status:'success',quota_policy:saved,revision:'fixture-after-save'};
};
function Fixture(){
    const [policy,setPolicy]=useState(saved),[mounted,setMounted]=useState(true);
    window.refreshObservation=()=>setPolicy({...saved});
    return <main className="mx-auto max-w-4xl p-5"><h1 className="text-2xl font-bold">AI 연결</h1><p className="mt-3 text-sm">프로젝트 한도 확인 시험 · 실제 설정을 바꾸지 않습니다.</p>
        <button onClick={()=>setMounted(value=>!value)} className="mt-3 p-2 underline">시험 화면 이동</button>
        {mounted&&<AiQuotaObservation policy={policy} configured={mode!=='no-key'} onRefresh={async()=>setPolicy({...saved})}/>}</main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);

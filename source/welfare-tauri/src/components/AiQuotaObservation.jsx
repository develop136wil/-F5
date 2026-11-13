import React,{useEffect,useRef,useState} from 'react';
import {welfareApi} from '../api/welfare';
import {useSettingsDraft} from '../hooks/useSettingsDraft';
import {settingsDrafts} from '../utils/settingsDrafts';
import {getErrorMessage} from '../utils/error';
import {QUOTA_MODELS,QUOTA_METRICS,quotaDraftFromPolicy,quotaObservationPayload,quotaPolicyStatusText} from '../utils/quotaObservation';

const draftKey='quotaObservationDraft';
export default function AiQuotaObservation({policy,configured,onRefresh}) {
    const [draft,setDraft]=useSettingsDraft(draftKey,()=>quotaDraftFromPolicy());
    const [confirmed,setConfirmed]=useState(false),[saving,setSaving]=useState(false);
    const [error,setError]=useState(''),[notice,setNotice]=useState('');
    const pending=useRef(false),mounted=useRef(true);
    useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
    useEffect(()=>{
        settingsDrafts.hydrate({[draftKey]:quotaDraftFromPolicy(policy)});
        setConfirmed(false);
    },[policy]);
    const edit=(model,key,value)=>{setDraft(previous=>({...previous,[model]:{...previous[model],[key]:value}}));setConfirmed(false);setNotice('');setError('');};
    async function save(){
        if(pending.current||!configured||policy===undefined)return;
        let payload;
        try{payload=quotaObservationPayload(draft,confirmed);}catch(e){setError(e.message);return;}
        const submitted=draft;
        pending.current=true;setSaving(true);setConfirmed(false);setError('');setNotice('');
        try{
            const result=await settingsDrafts.runWrite(()=>welfareApi.saveQuotaObservation(payload));
            if(result?.status!=='success'||result?.quota_policy?.state!=='current')throw Error('한도 확인 결과를 확인하지 못했습니다. 상태를 새로고침해주세요.');
            settingsDrafts.commit(draftKey,submitted,quotaDraftFromPolicy(result.quota_policy));
            if(mounted.current)setNotice('확인한 프로젝트 한도를 저장했습니다. 대기 작업은 직접 이어서 진행해주세요.');
            await onRefresh?.();
        }catch(e){if(mounted.current)setError(`한도 저장 실패: ${getErrorMessage(e)}`);}
        finally{pending.current=false;if(mounted.current)setSaving(false);}
    }
    return <section aria-labelledby="ai-quota-observation-title" className="mt-5 text-sm text-gray-700 dark:text-gray-300">
        <h3 id="ai-quota-observation-title" className="text-base font-semibold text-gray-900 dark:text-gray-100">프로젝트 한도 확인</h3>
        <p role="status" className="mt-2 leading-6">{quotaPolicyStatusText(policy)}</p>
        <details className="mt-3">
            <summary className="cursor-pointer py-2 font-medium text-blue-700 dark:text-blue-300">{policy?.state==='current'?'한도 다시 확인·수정':'확인한 한도 입력'}</summary>
            <p className="mt-2 leading-6">현재 연결한 키의 프로젝트를 <a href="https://aistudio.google.com/usage" target="_blank" rel="noopener noreferrer" className="text-blue-700 underline dark:text-blue-300">Google AI Studio 사용량·한도 화면</a>에서 선택해주세요. 각 모델의 <strong>한도(분모)</strong>를 입력합니다. 최근 28일 최대 사용량이나 사용한 횟수가 아닙니다.</p>
            <div className="mt-4 space-y-5">
                {QUOTA_MODELS.map(model=><fieldset key={model} disabled={saving||policy===undefined}>
                    <legend className="break-all font-medium text-gray-900 dark:text-gray-100">{model}</legend>
                    <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-3">
                        {QUOTA_METRICS.map(({key,label,max})=><label key={key} className="min-w-0 text-sm">{label}
                            <input type="text" inputMode="numeric" value={draft[model][key]} onChange={event=>edit(model,key,event.target.value)} aria-label={`${model} ${label}`} aria-describedby="quota-observation-help" className="mt-1 block min-h-11 w-full min-w-0 rounded-lg border border-gray-300 bg-white px-3 dark:border-gray-600 dark:bg-gray-900" />
                        </label>)}
                    </div>
                    {policy?.state==='current'&&policy.effective?.[model]&&<p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">분당 요청 보호: {QUOTA_METRICS.filter(({key})=>key!=='rpd'&&Number.isFinite(policy.effective[model][key])).map(({key})=>`${key.toUpperCase()} ${policy.effective[model][key].toLocaleString('ko-KR')}`).join(' · ')}</p>}
                </fieldset>)}
            </div>
            <p id="quota-observation-help" className="mt-4 leading-6 text-gray-500 dark:text-gray-400">프로젝트·모델별 한도는 달라질 수 있습니다. 앱은 확인한 분당 한도보다 여유를 두어 요청합니다. 앱의 RPM 집계는 보호용 요청 단위이며, TPM은 입력량 추정치입니다. 하루 사용 한도는 Google의 응답을 따릅니다. 다른 앱·기기의 사용량은 알 수 없어 Google의 실제 제한이 더 낮으면 429 응답 후 기다립니다. 무료 사용이나 비용 상한을 보장하지 않습니다.</p>
            <label className="mt-3 flex min-h-11 items-start gap-2 py-2 leading-6"><input type="checkbox" checked={confirmed} disabled={saving||policy===undefined||!configured} onChange={event=>setConfirmed(event.target.checked)} className="mt-1 h-4 w-4 shrink-0" /><span>현재 연결한 키의 프로젝트에서 두 모델의 한도(분모)를 직접 확인했습니다.</span></label>
            {!configured&&<p className="mt-1 text-amber-800 dark:text-amber-200">한도를 저장하려면 먼저 Gemini 키를 연결해주세요.</p>}
            <button type="button" onClick={save} disabled={saving||policy===undefined||!configured||!confirmed} className="mt-2 min-h-11 rounded-lg border border-blue-200 px-4 text-sm font-semibold text-blue-700 disabled:opacity-50 dark:border-blue-800 dark:text-blue-300">{saving?'한도 저장 중…':'확인한 한도 저장'}</button>
        </details>
        {error&&<p role="alert" className="mt-3 break-words text-red-700 dark:text-red-300">{error}</p>}
        {notice&&<p role="status" className="mt-3 leading-6 text-emerald-700 dark:text-emerald-300">{notice}</p>}
        <button type="button" disabled={saving} onClick={()=>onRefresh?.()} className="mt-2 min-h-11 py-2 text-sm text-gray-500 underline underline-offset-4 disabled:opacity-50">저장된 한도 새로고침</button>
    </section>;
}

import React, {useEffect, useState, useRef} from 'react';
import {Link} from 'react-router-dom';
import {welfareApi} from '../api/welfare';
import {getErrorMessage} from '../utils/error';
import NewsProfileRunPanel from './NewsProfileRunPanel';
import NewsProfileResults from './NewsProfileResults';
import NewsConditionCardSummary, {conditionCardClass} from './NewsConditionCardSummary';

// Only mounted after an explicit click. Selecting a condition never starts work.
function TargetChooser({actionBusy,onResults}) {
    const [items,setItems]=useState([]), [error,setError]=useState(''), [loading,setLoading]=useState(true);
    const [selected,setSelected]=useState(''), [result,setResult]=useState(null), [refresh,setRefresh]=useState(0);
    useEffect(()=>{
        let cancelled=false;
        setLoading(true);setError('');
        welfareApi.getNewsProfiles().then(data=>{if(!cancelled)setItems((data.items||[]).filter(p=>p.profile_id!=='default-news'&&!p.protected&&!p.archived));})
            .catch(e=>{if(!cancelled)setError(getErrorMessage(e));}).finally(()=>{if(!cancelled)setLoading(false);});
        return()=>{cancelled=true;};
    },[refresh]);
    const profile=items.find(p=>p.profile_id===selected);
    return <div className="mt-4 space-y-4">
        <p className="text-sm text-gray-500">보관한 조건으로 따로 찾습니다. 현재 수집 조건·예약은 바뀌지 않습니다.</p>
        {error && <p role="alert" className="text-sm text-red-600">{error} <button className="underline" onClick={()=>setRefresh(n=>n+1)}>다시 확인</button></p>}
        <fieldset disabled={loading||Boolean(error)||actionBusy} className="min-w-0">
            <legend className="mb-3 text-base font-semibold">찾을 대상 조건</legend>
            {loading&&<p role="status" className="text-sm text-gray-500">조건 불러오는 중…</p>}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {items.map(p=><label key={p.profile_id} className={`${conditionCardClass} cursor-pointer ${selected===p.profile_id?'ring-2 ring-blue-600 bg-blue-50/50 dark:bg-blue-950/30':''} focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-blue-600`}>
                    <span className="mb-2 flex items-center gap-2 text-sm text-blue-700 dark:text-blue-300">
                        <input type="radio" name="news-condition-choice" value={p.profile_id} checked={selected===p.profile_id} onChange={()=>{setSelected(p.profile_id);setResult(null);}} aria-label={p.name} className="h-4 w-4 accent-blue-600" />
                        {selected===p.profile_id?'✓ 선택됨':'이 조건 선택'}
                    </span>
                    <NewsConditionCardSummary profile={p}/>
                </label>)}
            </div>
        </fieldset>
        <Link to="/settings?tab=keywords" className="inline-block text-sm text-blue-600 underline">저장한 다른 조건 관리</Link>
        {!loading&&!error&&!items.length&&<p className="text-sm text-gray-500">보관한 조건이 없습니다. 설정의 ‘저장한 다른 조건’에서 새 조건을 만들어주세요.</p>}
        {profile&&<NewsProfileRunPanel key={profile.profile_id} profile={profile} actionBusy={actionBusy}
            onClose={()=>{setSelected('');setResult(null);}} onResults={runId=>onResults ? onResults(profile.profile_id,profile.name) : setResult({profileId:profile.profile_id,runId})}/>}
        {profile&&<button type="button" className="text-sm text-blue-600 underline" onClick={()=>onResults ? onResults(profile.profile_id,profile.name) : setResult({profileId:profile.profile_id})}>이 조건의 찾은 소식 보기</button>}
        {!onResults&&result&&<NewsProfileResults key={`${result.profileId}:${result.runId||'all'}`} {...result} actionBusy={actionBusy} onClose={()=>setResult(null)}/>}
    </div>;
}

function ConditionDialog({actionBusy,onClose,onResults}) {
    const ref=useRef(null);
    useEffect(()=>{
        const previous=document.activeElement;
        const dialog=ref.current;
        dialog.showModal();
        return()=>{dialog.close();previous?.focus?.();};
    },[]);
    return <dialog ref={ref} id="news-target-picker" aria-labelledby="news-target-title" onCancel={event=>{event.preventDefault();if(!actionBusy)onClose();}} className="m-auto max-h-[85vh] w-[min(800px,calc(100vw-32px))] overflow-y-auto rounded-2xl border border-gray-200 bg-white p-5 text-gray-900 shadow-xl backdrop:bg-slate-900/40 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100">
        <header className="flex items-center justify-between gap-3"><h2 id="news-target-title" className="text-lg font-semibold">다른 조건 선택</h2><button type="button" disabled={actionBusy} onClick={onClose} className="min-h-10 rounded-lg border px-3 text-sm disabled:opacity-50">닫기</button></header>
        <TargetChooser actionBusy={actionBusy} onResults={onResults}/>
    </dialog>;
}

export default function NewsTargetCollection({actionBusy=false,onResults}) {
    const [open,setOpen]=useState(false);
    return <section className="text-sm dark:text-gray-100">
        <div className="flex flex-wrap items-center justify-between gap-3">
            <button type="button" aria-haspopup="dialog" aria-expanded={open} aria-controls="news-target-picker" disabled={actionBusy} onClick={()=>setOpen(true)}
                className="min-h-10 rounded-lg border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 disabled:opacity-50">다른 조건 선택</button>
        </div>
        {open&&<ConditionDialog actionBusy={actionBusy} onClose={()=>setOpen(false)} onResults={onResults ? (id,name)=>{setOpen(false);onResults(id,name);} : undefined}/>}
    </section>;
}

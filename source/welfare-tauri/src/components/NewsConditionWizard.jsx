import React, {useId, useLayoutEffect, useRef, useState} from 'react';
import {focusSetupStep} from '../utils/focusSetupStep';
import OptionalAgeMonthInput from './OptionalAgeMonthInput';
import ConditionRegionPicker from './ConditionRegionPicker';
import {newCollectionCondition} from '../utils/newCollectionCondition';
import {ageHint} from '../utils/newsProfileForm';
import {validateConditionDraft} from '../utils/conditionWizard';
import {editableCategoryOptions} from '../utils/conditionCategories';
import SetupSteps from './ui/SetupSteps';
import {Button} from './ui/Button';
import NewsConditionDetails, {conditionDetailsSummary} from './NewsConditionDetails';
import NewsPublicationPeriod from './NewsPublicationPeriod';
import {publicationSettings} from '../utils/newsPublication';

export default function NewsConditionWizard({preset, initialSettings, onSave, onClose, onDraftChange, value, ariaLabel, initialStep=0, detailsOpen=false}) {
    const inputId=useId();
    const [step,setStep]=useState(initialStep), [localDraft,setDraft]=useState(()=>({...newCollectionCondition(null),
        life_cycles:preset?.life_cycles || [], characteristics:preset?.characteristics || [],
        ...structuredClone(initialSettings || {}),
        min_age_months:initialSettings?.min_age_months ?? '',max_age_months:initialSettings?.max_age_months ?? ''}));
    const draft=value?{...value,min_age_months:value.min_age_months??'',max_age_months:value.max_age_months??''}:localDraft;
    const [busy,setBusy]=useState(false), [error,setError]=useState('');
    const pending=useRef(false);
    const headingRef=useRef(null),previousStep=useRef(step);
    useLayoutEffect(()=>{if(detailsOpen)focusSetupStep(headingRef.current);},[]);
    useLayoutEffect(()=>{if(previousStep.current!==step){previousStep.current=step;focusSetupStep(headingRef.current);}},[step]);
    const change=(key,value)=>{const next={...draft,[key]:value};setDraft(next);onDraftChange?.(next);};
    const toggle=(key,value)=>change(key,draft[key].includes(value)?draft[key].filter(x=>x!==value):[...draft[key],value]);
    const groups=[['life_cycles'],['characteristics']];
    const options={life_cycles:editableCategoryOptions('life_cycles',draft.life_cycles),characteristics:editableCategoryOptions('characteristics',draft.characteristics)};
    function next(){try{validateConditionDraft(draft);setError('');setStep(s=>Math.min(3,s+1));}catch(e){setError(e.message);}}
    async function save(){if(pending.current)return;pending.current=true;setBusy(true);setError('');try{
        const checked={...validateConditionDraft(draft), ...publicationSettings(draft)};
        await onSave({...draft,...checked,name:initialSettings ? draft.name : `${checked.region} · ${[...draft.life_cycles,...draft.characteristics].join('·') || '전체 대상'}`.slice(0,90)});
    }catch(e){setError(e.message || '저장하지 못했습니다. 다시 시도해주세요.');}finally{pending.current=false;setBusy(false);}}
    return <section aria-label={ariaLabel || (initialSettings ? '수집 조건 수정' : '새 수집 조건 만들기')} className="max-w-3xl space-y-6 pt-4 text-sm">
        <SetupSteps labels={['지역','대상','나이','확인']} current={step} label="수집 조건 설정 단계"/>
        <h3 ref={headingRef} tabIndex={-1} className="ui-subtitle focus:outline-none">{['어느 지역의 소식을 찾을까요?','누구를 위한 소식을 찾을까요?','나이 범위를 정해주세요','저장할 조건을 확인해주세요'][step]}</h3>
        {step===0&&<ConditionRegionPicker value={draft.region} onChange={v=>change('region',v)}/>}
        {step===1&&<div className="space-y-5">{groups.map(([key])=><fieldset key={key}><legend className="mb-2 font-medium">{key==='life_cycles'?'생애주기':key==='characteristics'?'가구상황':'관심주제'}</legend><p className="text-xs text-gray-500 mb-2">{key==='life_cycles'?'선택하지 않으면 생애주기를 제한하지 않습니다.':'일반은 모든 정보에 기본 적용됩니다. 추가 대상을 선택하면 그 대상으로 좁힙니다.'}</p><div className="flex flex-wrap gap-2">{options[key].map(value=><button type="button" key={value} aria-pressed={draft[key].includes(value)} onClick={()=>toggle(key,value)} className={`border rounded-lg px-3 py-2 ${draft[key].includes(value)?'bg-blue-50 text-blue-800 border-blue-500':'text-gray-600 dark:text-gray-200'}`}>{draft[key].includes(value)?'✓ ':''}{value}</button>)}</div></fieldset>)}</div>}
        {step===2&&<><OptionalAgeMonthInput label="시작 나이" idPrefix={`${inputId}-min`} value={draft.min_age_months} onChange={v=>change('min_age_months',v)}/><OptionalAgeMonthInput label="종료 나이" idPrefix={`${inputId}-max`} value={draft.max_age_months} onChange={v=>change('max_age_months',v)}/><p className="text-sm text-gray-500">선택한 생애주기와 나이 범위를 함께 확인합니다.</p></>}
        {step===3&&<div className="space-y-2 rounded-xl bg-gray-50 p-4 text-sm dark:bg-gray-900/40"><p>지역: {draft.region}</p><p>생애주기: {draft.life_cycles.join(' · ') || '제한 없음'}</p><p>가구상황: {draft.characteristics.join(' · ') || '일반 · 제한 없음'}</p><p>나이: {ageHint(draft.min_age_months)} ~ {ageHint(draft.max_age_months)}</p><p>세부 설정: {conditionDetailsSummary(draft)}</p><p className="ui-meta">최종 저장 후 다음 수집부터 적용됩니다. 저장만으로 수집이나 AI 분석은 시작하지 않습니다.</p></div>}
        <NewsConditionDetails value={draft} onChange={change} defaultOpen={detailsOpen}/>
        {step===3&&<NewsPublicationPeriod value={draft} onChange={change} disabled={busy}/>}
        {error&&<p role="alert" className="text-red-700">{error}</p>}
        <div className="flex flex-wrap gap-3"><Button type="button" variant="ghost" disabled={busy} onClick={onClose}>취소</Button>{step>0&&<Button type="button" variant="outline" disabled={busy} onClick={()=>setStep(s=>s-1)}>이전</Button>}<Button type="button" disabled={busy} onClick={step===3?save:next}>{busy?'저장 중…':step===3?'최종 저장':'다음'}</Button></div>
    </section>;
}

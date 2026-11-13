import React, {useEffect, useState} from 'react';
import {useSettingsDraft} from '../hooks/useSettingsDraft';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {welfareApi} from '../api/welfare';
import {settingsDrafts} from '../utils/settingsDrafts';
import {newCollectionCondition} from '../utils/newCollectionCondition';
import NewsConditionWizard from './NewsConditionWizard';
import {ageHint} from '../utils/newsProfileForm';
import {ChevronRight} from 'lucide-react';
import {conditionDetailsSummary} from './NewsConditionDetails';
import {publicationLabel} from '../utils/newsPublication';

export default function DefaultNewsConditionEditor({onSaved}) {
    const client=useQueryClient();
    const query=useQuery({queryKey:['defaultNewsCondition'],queryFn:welfareApi.getDefaultNewsCondition});
    const [editing,setEditing]=useState(false);
    const [formEpoch,setFormEpoch]=useState(0);
    if(query.isLoading)return <p role="status">수집 조건을 확인하고 있습니다.</p>;
    if(query.isError)return <p role="alert">수집 조건을 읽지 못했습니다. <button onClick={()=>query.refetch()} className="underline">다시 확인</button></p>;
    const profile=query.data?.profile;
    const initial=newCollectionCondition(null);
    const saved=profile||query.data?.initial_settings;
    if(saved)for(const key of Object.keys(initial))if(Object.hasOwn(saved,key))initial[key]=saved[key];
    initial.name=profile?.name||'새 소식 기본 수집';
    return <div className="space-y-4">
        <p className="ui-description">새 소식과 자동 수집에 사용할 조건을 정합니다. 정책 최신화에는 적용되지 않습니다.</p>
        {!profile&&saved&&Object.keys(saved).length>0&&<p className="ui-meta">이전에 저장한 수집 설정을 표시했습니다. 지역·나이·세부 설정을 확인하고 최종 저장해야 다음 수집에 적용됩니다. 기존 설정은 자동으로 바꾸지 않습니다.</p>}
        {profile && <>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 rounded-xl bg-gray-50 p-4 text-sm dark:bg-gray-900/40">
                <dt className="text-gray-500 dark:text-gray-400">지역</dt><dd className="min-w-0 break-words font-medium">{profile.region||'지역 제한 없음'}</dd>
                <dt className="text-gray-500 dark:text-gray-400">대상</dt><dd className="min-w-0 break-words">{[...profile.life_cycles,...profile.characteristics].join(' · ')||'대상 제한 없음'}</dd>
                <dt className="text-gray-500 dark:text-gray-400">나이</dt><dd className="min-w-0 break-words">{ageHint(profile.min_age_months??'')} ~ {ageHint(profile.max_age_months??'')}</dd>
                <dt className="text-gray-500 dark:text-gray-400">게시 기간</dt><dd>{publicationLabel(profile)}</dd>
            </dl>
            {!editing && <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <button type="button" onClick={()=>setEditing('scope')} className="ui-disclosure flex min-h-20 w-full items-center justify-between gap-3 px-4 py-3 text-left hover:border-teal-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-600">
                    <span className="min-w-0 space-y-1"><span className="ui-subtitle block">수집 조건 변경</span><span className="ui-meta block">지역 · 대상 · 나이 · 게시 기간</span></span>
                    <ChevronRight size={20} aria-hidden="true" className="shrink-0 text-gray-500"/>
                </button>
                <button type="button" onClick={()=>setEditing('details')} className="ui-disclosure flex min-h-20 w-full items-center justify-between gap-3 px-4 py-3 text-left hover:border-teal-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-600">
                    <span className="min-w-0 space-y-1"><span className="ui-subtitle block">수집 세부 설정</span><span className="ui-meta block">{conditionDetailsSummary(profile)}</span></span>
                    <ChevronRight size={20} aria-hidden="true" className="shrink-0 text-gray-500"/>
                </button>
            </div>}
        </>}
        {(!profile||editing) && <DefaultConditionForm key={formEpoch} initial={initial} revision={profile?.revision||0} detailsOpen={editing==='details'} onClose={()=>{setEditing(false);setFormEpoch(value=>value+1);}}
            onSave={async (value,revision)=>{
                const result=await settingsDrafts.runWrite(()=>welfareApi.saveDefaultNewsCondition(value,revision));
                client.setQueryData(['defaultNewsCondition'],result);
                await client.invalidateQueries({queryKey:['newsProfiles']});
                setEditing(false);onSaved?.();
                return result;
            }}/ >}
    </div>;
}

function DefaultConditionForm({initial,revision,onSave,onClose,detailsOpen}) {
    const [draft,setDraft]=useSettingsDraft('defaultNewsConditionDraft',{settings:initial,revision});
    const [busy,setBusy]=useState(false);
    useEffect(()=>{settingsDrafts.hydrate({defaultNewsConditionDraft:{settings:initial,revision}});},[]);
    return <fieldset disabled={busy} className="min-w-0"><NewsConditionWizard ariaLabel={revision?'수집 조건 수정':'새 수집 조건 만들기'} initialStep={detailsOpen?3:0} detailsOpen={detailsOpen} initialSettings={draft.settings} value={draft.settings} onDraftChange={value=>setDraft({...draft,settings:value})}
        onClose={()=>{settingsDrafts.discard({defaultNewsConditionDraft:settingsDrafts.capture().defaultNewsConditionDraft});onClose();}}
        onSave={async value=>{const submitted=settingsDrafts.get('defaultNewsConditionDraft');setBusy(true);try{const result=await onSave(value,submitted.revision);settingsDrafts.commit('defaultNewsConditionDraft',submitted,{settings:value,revision:result.profile.revision});}finally{setBusy(false);}}}/></fieldset>;
}

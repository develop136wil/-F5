import React, {useEffect, useState} from 'react';
import {welfareApi} from '../api/welfare';
import {editableSettings} from '../utils/newsProfileForm';
import {settingsDrafts} from '../utils/settingsDrafts';
import {profileDraftKey, profileDraftValue} from '../utils/profileDraft';
import {profileResults} from '../utils/profileResults';
import {getErrorMessage} from '../utils/error';
import NewsConditionWizard from './NewsConditionWizard';
import NewsProfileEditor from './NewsProfileEditor';

// Existing advanced drafts retain their original editor and conflict safeguards.
export default function NewsConditionEditor({profileId,initialTaxonomy,onClose,onSaved}) {
    const key=profileDraftKey(profileId);
    const [advanced,setAdvanced]=useState(()=>settingsDrafts.dirty(key));
    const [profile,setProfile]=useState(null),[error,setError]=useState('');
    useEffect(()=>{
        let alive=true;
        welfareApi.getNewsProfile(profileId).then(({profile:p})=>{if(alive)setProfile(p);})
            .catch(e=>{if(alive)setError(getErrorMessage(e));});
        return()=>{alive=false;};
    },[profileId]);
    if(advanced)return <NewsProfileEditor profileId={profileId} initialTaxonomy={initialTaxonomy} onClose={onClose} onSaved={onSaved}/>;
    if(error)return <div role="alert">{error} <button onClick={onClose}>닫기</button></div>;
    if(!profile)return <p role="status">저장한 조건을 불러오는 중입니다…</p>;
    return <>
        <NewsConditionWizard initialSettings={editableSettings(profile)} onClose={onClose} onSave={async value=>{
            const result=await settingsDrafts.runWrite(async()=>{
                if(settingsDrafts.dirty(key))throw Error('다른 화면에서 수정한 초안이 있습니다. 닫은 뒤 다시 열어 초안을 확인해주세요.');
                return welfareApi.saveNewsProfile(profileId,profile.revision,value);
            });
            const saved={...profile,...value,revision:result.revision};
            const snapshot=settingsDrafts.capture();
            settingsDrafts.hydrate({[key]:profileDraftValue(saved)},snapshot);
            profileResults.observe(saved);
            onSaved();
        }}/>
        <p className="ui-meta mt-4">이름·개별 기관 사이트는 목록의 ‘추가 설정’에서 변경할 수 있습니다. 기존 값은 유지됩니다.</p>
    </>;
}

import React, { useEffect, useRef, useState } from 'react';
import { welfareApi } from '../api/welfare';
import { getErrorMessage } from '../utils/error';
import { parseTags, validateAges, targetChanged, applyProfilePreset, toggleTag } from '../utils/newsProfileForm';
import { useSettingsDraft } from '../hooks/useSettingsDraft';
import { settingsDrafts } from '../utils/settingsDrafts';
import { profileDraftKey, profileDraftValue, commitProfileDraft } from '../utils/profileDraft';
import { profileResults } from '../utils/profileResults';
import { toast } from 'sonner';
import {Link} from 'react-router-dom';
import OptionalAgeMonthInput from './OptionalAgeMonthInput';
import {editableCategoryOptions} from '../utils/conditionCategories';
import {missingProfileSiteIds, normalizeSiteIds, toggleProfileSite} from '../utils/newsProfileSites';
import NewsPublicationPeriod from './NewsPublicationPeriod';
import {publicationSettings} from '../utils/newsPublication';

const inputClass = 'mt-1 w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 p-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500';
const labels = { region: '지역', age: '나이', life_cycle: '대상 연령대', characteristics: '가구·대상 조건', filter_disabled: '대상 조건 확인 꺼짐' };
const outcomes = { match: '적합', excluded: '제외', review: '검토 필요' };

export default function NewsProfileEditor({ profileId, onClose, onSaved, initialTaxonomy = null }) {
    const draftKey = profileDraftKey(profileId);
    const [entry, setEntry] = useSettingsDraft(draftKey, null);
    const profile = entry?.profile, draft = entry?.draft;
    const setDraft = value => setEntry(previous => ({...previous, draft:value}));
    const [remoteProfile, setRemoteProfile] = useState(null);
    const [discardRequest, setDiscardRequest] = useState(null);
    const conflict = remoteProfile && profile && remoteProfile.revision > profile.revision;
    const [taxonomy, setTaxonomy] = useState(initialTaxonomy);
    const [facts, setFacts] = useState({ region: '', cycles: '', traits: '', min: '', max: '' });
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [preview, setPreview] = useState(null);
    const [sites, setSites] = useState(null), [sitesError, setSitesError] = useState(''), [sitesLoading, setSitesLoading] = useState(true);
    const mounted = useRef(false), pending = useRef(false);
    useEffect(() => {
        let cancelled = false;
        const request = settingsDrafts.capture();
        mounted.current = true;
        welfareApi.getNewsProfile(profileId).then(({ profile: p }) => {
            if (cancelled) return;
            setRemoteProfile(p);
            profileResults.observe(p);
            settingsDrafts.hydrate({[draftKey]:profileDraftValue(p)}, request);
        }).catch(e => { if (!cancelled) setError(getErrorMessage(e)); });
        if (!initialTaxonomy) welfareApi.getNewsProfileTaxonomy().then(value => {
            if (!cancelled) setTaxonomy(value);
        }).catch(() => {});
        welfareApi.getSites().then(value => {
            if (!cancelled) { setSites(Array.isArray(value) ? value : []); setSitesError(''); }
        }).catch(() => { if (!cancelled) setSitesError('수집 사이트 목록을 불러오지 못했습니다.'); })
            .finally(() => { if (!cancelled) setSitesLoading(false); });
        return () => { cancelled = true; mounted.current = false; };
    }, [profileId]);
    function settings() {
        const ages = validateAges(draft.min_age_months, draft.max_age_months);
        if (!draft.name.trim() || !draft.region.trim()) throw new Error('이름과 지역을 입력해주세요.');
        const sources = ['sites'];
        const value = { ...draft, name: draft.name.trim(), region: draft.region.trim(), sources,
            site_ids:normalizeSiteIds(draft.site_ids),
            min_age_months: ages.start_age_months, max_age_months: ages.end_age_months,
            ...Object.fromEntries(['life_cycles', 'characteristics', 'interest_topics', 'keywords', 'excluded_keywords'].map(k => [k, parseTags(draft[k] || '')])) };
        return {...value, ...publicationSettings(draft)};
    }
    async function submit(save) {
        if (pending.current) return;
        pending.current = true; setBusy(true); setError(''); setPreview(null);
        try {
            const value = settings();
            if (save) {
                if (!remoteProfile || conflict) throw Error('저장된 버전을 먼저 확인해주세요. 새 버전이 있다면 내용을 비교한 뒤 다시 편집해주세요.');
                const result = await settingsDrafts.runWrite(() => welfareApi.saveNewsProfile(profileId, profile.revision, value));
                const savedProfile = {...profile, ...value, revision:result.revision};
                commitProfileDraft(settingsDrafts, draftKey, entry, savedProfile);
                profileResults.observe(savedProfile);
                const message = `${value.name}: 조건을 저장했습니다. 현재 수집에는 적용되지 않습니다.`;
                profileResults.report(profileId, {ok:true, message});
                toast.success(message);
                if (mounted.current) {
                    setRemoteProfile(savedProfile);
                    if (!settingsDrafts.dirty(draftKey)) onSaved();
                }
            } else {
                const result = await welfareApi.previewNewsTarget(value, {
                    residence: parseTags(facts.region), life_cycle: parseTags(facts.cycles),
                    target_characteristics: parseTags(facts.traits), ...validateAges(facts.min, facts.max) });
                if (mounted.current) setPreview(result);
            }
        } catch (e) {
            const message = getErrorMessage(e);
            if (save) {
                const notice = `${draft?.name || '수집 조건'}: 저장하지 못했습니다. 입력은 유지됩니다. ${message}`;
                profileResults.report(profileId, {ok:false, message:notice});
                toast.error(notice, {duration:10000});
            }
            if (mounted.current) setError(message);
        }
        finally { pending.current = false; if (mounted.current) setBusy(false); }
    }
    return <div className="rounded-xl border border-blue-200 dark:border-blue-900 p-4 space-y-4 text-gray-800 dark:text-gray-100">
        <h3 className="font-semibold">수집 조건 수정</h3>
        {settingsDrafts.dirty(draftKey) && <p role="status" className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">미저장 내용이 있습니다. 다른 화면에 다녀와도 이어 쓸 수 있지만, 앱을 종료하면 사라집니다.</p>}
        {conflict && <p role="alert" className="text-sm text-amber-800">저장된 조건이 새 버전으로 바뀌었습니다. 현재 입력은 보존했습니다. 필요한 내용을 따로 복사한 뒤 미저장 변경을 버리면 최신 저장본을 불러옵니다.</p>}
        {error && <p role="alert" className="text-sm text-red-600 break-words">{error}</p>}
        {!draft ? <p className="text-sm">{error ? '편집을 닫고 다시 열어주세요.' : '설정 불러오는 중…'}</p> :
        <fieldset disabled={busy} className="space-y-4 disabled:opacity-60">
            <legend className="mb-3 font-medium text-sm">찾고 싶은 대상 정하기</legend>
            <NewsPublicationPeriod value={draft} onChange={(key,value)=>setDraft({...draft,[key]:value})} disabled={busy}/>
            {taxonomy && <section className="rounded-xl border border-blue-100 bg-blue-50/60 p-3 dark:border-blue-900/50 dark:bg-blue-950/20" aria-label="대상 기준 빠른 선택">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                        <h4 className="text-sm font-semibold text-blue-900 dark:text-blue-100">대상 범위 정하기</h4>
                        <p className="mt-1 text-xs leading-5 text-blue-800/80 dark:text-blue-200/80">찾고 싶은 대상과 지원 분야를 선택하세요. 필요한 줄만 선택해도 됩니다.</p>
                    </div>
                </div>
                <div className="mt-3 rounded-lg bg-white/80 p-3 text-xs leading-5 text-gray-700 dark:bg-gray-900/60 dark:text-gray-200">
                    <p><b>선택 예시:</b> ‘노년’ + ‘장애인’ + ‘주거’ → 노년층이면서 장애인 대상이고 주거와 관련된 자료를 확인합니다.</p>
                    <p className="mt-1">{taxonomy.combination_rule}</p>
                </div>
                <div className="mt-3 space-y-3">
                    {[
                        ['life_cycles', editableCategoryOptions('life_cycles', parseTags(draft.life_cycles || '')), '1. 누구를 찾나요?', '생애주기', '나이에 따른 단계입니다. 여러 개를 고르면 그중 하나에 맞으면 됩니다.'],
                        ['characteristics', editableCategoryOptions('characteristics', parseTags(draft.characteristics || '')), '2. 어떤 상황인가요?', '가구상황', '일반은 공통 속성입니다. 추가 대상을 선택하지 않으면 가구상황을 제한하지 않습니다.'],
                        ['interest_topics', taxonomy.interest_topics || [], '3. 어떤 도움이 필요한가요?', '관심주제', '건강·주거·돌봄 같은 지원 분야입니다. 비워두면 분야를 제한하지 않습니다.'],
                    ].map(([key, options, question, label, help]) => <div key={key} className="rounded-lg border border-blue-100 bg-white p-3 dark:border-blue-900/40 dark:bg-gray-900">
                        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                            <p className="text-sm font-semibold text-gray-900 dark:text-gray-100">{question}</p>
                            <span className="text-xs text-gray-500">{label}</span>
                        </div>
                        <p className="mt-1 text-xs leading-5 text-gray-600 dark:text-gray-300">{help}</p>
                        <div className="mt-2 flex flex-wrap gap-2">
                            {options.map(option => { const selected = parseTags(draft[key] || '').includes(option); return <button key={option} type="button" aria-pressed={selected}
                                className={selected ? 'rounded-full border border-blue-600 bg-blue-600 px-3 py-1.5 text-sm font-medium text-white' : 'rounded-full border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-700 hover:border-blue-300 hover:bg-blue-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200'}
                                onClick={() => { setDraft({...draft, [key]:toggleTag(draft[key], option)}); setPreview(null);  }}>
                                {option}
                            </button>;})}
                        </div>
                    </div>)}
                </div>
                <details className="mt-3 rounded-lg border border-blue-100 bg-white/70 p-3 dark:border-blue-900/40 dark:bg-gray-900/50">
                    <summary className="cursor-pointer text-sm font-medium">직접 대상 분류 추가하기</summary>
                    <p className="mt-2 text-xs leading-5 text-gray-500">쉼표로 나누어 입력하세요. 자체 분류는 등록한 기관 사이트의 대상 확인과 AI 분석에 사용됩니다.</p>
                    <div className="mt-2 grid grid-cols-[repeat(auto-fit,minmax(min(210px,100%),1fr))] gap-3">
                        {[['life_cycles','연령대 추가'], ['characteristics','가구·대상 조건 추가'], ['interest_topics','지원 분야 추가']].map(([key,label]) =>
                            <label key={key} className="text-sm">{label}<input className={inputClass} value={draft[key] || ''} maxLength={5000}
                                onChange={e => { setDraft({...draft,[key]:e.target.value}); setPreview(null);  }}/></label>)}
                    </div>
                </details>
                <div className="mt-3 border-t border-blue-100 pt-3 dark:border-blue-900/40">
                    <p className="text-xs leading-5 text-blue-900 dark:text-blue-100"><b>빠른 추천 3개</b> · 자주 쓰는 조합 예시입니다. 임신·출산, 아동, 청소년, 청년 등은 위 버튼에서 자유롭게 선택하세요.</p>
                <div className="mt-3 flex flex-wrap gap-2">
                    {taxonomy.presets.map(preset => <button key={preset.id} type="button"
                        className="rounded-full border border-blue-200 bg-white px-3 py-1.5 text-sm font-medium text-blue-700 hover:bg-blue-100 dark:border-blue-800 dark:bg-gray-900 dark:text-blue-300"
                        onClick={() => { setDraft(applyProfilePreset(draft, preset)); setPreview(null);  }}>
                        {preset.label}
                    </button>)}
                </div>
                </div>
            </section>}
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(220px,100%),1fr))] gap-3">
                {[['name','조건 이름'], ['region','대상 지역 (제한 없으면 전국)'], ['keywords','추가로 찾을 단어 (쉼표로 구분)'], ['excluded_keywords','제외할 단어 (쉼표로 구분)']].map(([key,label]) =>
                    <label key={key} className="text-sm">{label}<input className={inputClass} value={draft[key]} maxLength={key === 'name' || key === 'region' ? 100 : 5000}
                        placeholder={key === 'region' ? '예: 도봉구 또는 전국' : undefined}
                        inputMode={key.endsWith('_months') ? 'numeric' : undefined}
                        onChange={e => { setDraft({ ...draft, [key]: e.target.value }); setPreview(null);  }} />
                        </label>)}
            </div>
            <p className="text-xs leading-5 text-gray-500">지역은 한 곳만 입력합니다. 예: <b>도봉구</b>(구 단위) · <b>서울특별시</b>(시 전체) · <b>전국</b>(지역 제한 없음)</p>
            <section aria-label="대상 나이 범위" className="space-y-2">
                <h4 className="text-sm font-medium">대상 나이</h4>
                <p className="text-xs text-gray-500">필요한 경계만 지정할 수 있습니다. 두 항목이 모두 ‘제한 없음’이면 모든 나이를 확인합니다.</p>
                <div className="grid grid-cols-[repeat(auto-fit,minmax(min(250px,100%),1fr))] gap-3">
                    <OptionalAgeMonthInput label="시작 나이" value={draft.min_age_months} onChange={value=>{setDraft({...draft,min_age_months:value});setPreview(null);}} idPrefix="profile-min-age" suffix="부터"/>
                    <OptionalAgeMonthInput label="종료 나이" value={draft.max_age_months} onChange={value=>{setDraft({...draft,max_age_months:value});setPreview(null);}} idPrefix="profile-max-age" suffix="까지"/>
                </div>
                <p className="text-xs leading-5 text-gray-500">예: 시작 나이 <b>0세 2개월</b>, 종료 나이 <b>0세 6개월</b> → 2~6개월 대상 자료를 확인합니다.</p>
                {(()=>{try{validateAges(draft.min_age_months,draft.max_age_months);return null;}catch(e){return <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">{e.message}</p>;}})()}
            </section>
            <details className="rounded-xl border border-gray-200 p-3 dark:border-gray-700">
                <summary className="cursor-pointer text-sm font-medium">수집할 기관 사이트 · {normalizeSiteIds(draft.site_ids).length}곳 선택</summary>
                <div className="mt-3 space-y-3">
                    <p className="text-xs text-gray-500">이 조건으로 별도 수집할 기관만 선택하세요. 기본 수집이나 다른 조건의 사이트 선택은 바뀌지 않습니다.</p>
                    {sitesLoading && <p role="status" className="text-sm text-gray-500">사이트 불러오는 중…</p>}
                    {sitesError && <p role="alert" className="text-sm text-amber-700">{sitesError} 편집을 닫았다가 다시 열거나 <Link to="/sites" className="underline">사이트 등록·관리</Link>에서 확인해주세요.</p>}
                    {!sitesLoading && !sitesError && sites?.length===0 && <p className="text-sm text-gray-500">등록된 사이트가 없습니다. <Link to="/sites" className="text-blue-700 underline dark:text-blue-300">사이트 등록·관리</Link>에서 먼저 추가해주세요.</p>}
                    {sites?.length>0 && <>
                        <div className="flex flex-wrap gap-3 text-xs">
                            <button type="button" className="min-h-9 underline" onClick={()=>setDraft({...draft,site_ids:sites.map(site=>String(site.id))})}>모두 선택</button>
                            <button type="button" className="min-h-9 underline" onClick={()=>setDraft({...draft,site_ids:[]})}>전체 해제</button>
                            <Link to="/sites" className="inline-flex min-h-9 items-center text-blue-700 underline dark:text-blue-300">사이트 등록·관리</Link>
                        </div>
                        <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg bg-gray-50 p-2 dark:bg-gray-800/50">
                            {sites.map(site=>{const id=String(site.id),checked=normalizeSiteIds(draft.site_ids).includes(id);return <label key={id} className="flex min-h-10 items-center gap-3 rounded-lg px-2 py-1.5 text-sm hover:bg-white dark:hover:bg-gray-800">
                                <input type="checkbox" checked={checked} onChange={e=>setDraft({...draft,site_ids:toggleProfileSite(draft.site_ids,id,e.target.checked)})} className="h-4 w-4 accent-blue-600"/>
                                <span className="min-w-0 break-words">{site.name || `사이트 ${id}`}</span>
                            </label>;})}
                        </div>
                    </>}
                    {sites!==null && !sitesError && missingProfileSiteIds(draft.site_ids,sites).length>0 && <div role="alert" className="rounded-lg bg-amber-50 p-3 text-sm text-amber-800 dark:bg-amber-950/20 dark:text-amber-200">
                        <p>삭제되었거나 불러오지 못한 사이트 {missingProfileSiteIds(draft.site_ids,sites).length}곳이 이 조건에 남아 있습니다.</p>
                        <button type="button" className="mt-2 underline" onClick={()=>setDraft({...draft,site_ids:normalizeSiteIds(draft.site_ids).filter(id=>!missingProfileSiteIds(draft.site_ids,sites).includes(id))})}>연결 끊긴 사이트 제거</button>
                    </div>}
                    {!sitesLoading && !sitesError && normalizeSiteIds(draft.site_ids).length===0 && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">기관 사이트의 소식도 찾으려면 수집할 사이트를 선택해주세요.</p>}
                </div>
            </details>
            <details className="space-y-2 text-sm"><summary className="cursor-pointer font-medium">대상 분류와 나이 조건 도움말</summary>
            {taxonomy && <div className="space-y-2 text-sm text-gray-600 dark:text-gray-300">
                <p><b>생애주기:</b> {taxonomy.life_cycles.join(' · ')}</p>
                <p><b>가구상황:</b> {taxonomy.household_situations.join(' · ')}</p>
                <p><b>관심주제:</b> {(taxonomy.interest_topics || []).join(' · ')}</p>
            </div>}
            <p className="text-sm text-gray-600 dark:text-gray-300">연령대와 나이를 모두 입력하면 두 조건을 함께 확인합니다. 나이를 제한하지 않으려면 시작·종료 연령을 모두 비워두세요. ‘전생애’는 모든 연령대를 뜻합니다.</p>
            <p className="text-sm text-gray-600 dark:text-gray-300">같은 칸에 여러 대상을 넣으면 그중 하나에 해당하는 정보를 찾습니다. 생애주기와 가구상황은 함께 확인합니다. 실제 지원 가능 여부는 정책별 나이·소득·거주지 조건을 확인해야 합니다.</p>
            </details>
            {targetChanged(profile, draft) && <p role="status" className="text-sm text-amber-800 dark:text-amber-200">대상이 바뀌었습니다. ‘AI 분석 설명’을 열어 내용도 맞는지 확인해주세요.</p>}
            <details className="rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3 space-y-3" aria-label="AI 분석 기준 확인">
                <summary className="cursor-pointer text-sm font-medium">AI 분석 설명 (필요할 때 수정)</summary>
                <p className="text-sm text-gray-600 dark:text-gray-300">‘영유아만 찾기’처럼 대상과 맞지 않는 요청은 수정해주세요. 문장의 적합성을 자동 검증하지는 않습니다.</p>
                <label className="block text-sm">AI에게 추가로 부탁할 내용 (선택)
                    <textarea className={inputClass} rows={4} maxLength={10000} value={draft.ai_prompt}
                        onChange={e => { setDraft({ ...draft, ai_prompt: e.target.value }); setPreview(null); }} />
                    <span className="block mt-1 text-xs text-gray-600 dark:text-gray-300">비워두면 추가 요청을 넣지 않습니다. 아래 예시 확인에서는 이 내용을 사용하지 않습니다.</span>
                </label>
                <label className="block text-sm">찾고 싶은 정보에 대한 설명
                    <textarea className={inputClass} rows={3} maxLength={5000} value={draft.semantic_text}
                        onChange={e => { setDraft({ ...draft, semantic_text: e.target.value }); setPreview(null); }} />
                    <span className="block mt-1 text-xs text-gray-600 dark:text-gray-300">표현이 달라도 비슷한 정보를 찾기 위한 기준 문장입니다. 예: ‘노년층의 생활·돌봄·주거 지원 소식’.</span>
                </label>
            </details>
            <details><summary className="cursor-pointer text-sm font-medium rounded focus-visible:outline-blue-500">예시로 조건 확인하기 (AI 사용 없음)</summary>
                <p className="text-sm text-gray-600 dark:text-gray-300 mt-2">예시 정책에 적힌 조건을 입력하세요. 이 결과는 지원 자격 확정이나 실제 검색 품질 평가가 아닙니다. 정보가 부족하면 ‘검토 필요’로 표시합니다.</p>
                <div className="grid grid-cols-[repeat(auto-fit,minmax(min(220px,100%),1fr))] gap-3 mt-3">
                    {[['region','예시 정책의 지역'], ['cycles','예시 정책의 연령대'], ['traits','예시 정책의 가구·대상 조건'], ['min','예시 시작 연령 (개월)'], ['max','예시 종료 연령 (개월)']].map(([key,label]) =>
                        <label className="text-sm" key={key}>{label}<input className={inputClass} value={facts[key]} maxLength={1000}
                            onChange={e => { setFacts({ ...facts, [key]: e.target.value }); setPreview(null); }} /></label>)}
                </div>
                <button type="button" className="mt-3 rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm" onClick={() => submit(false)}>조건 확인</button>
            </details>
            {preview && <div role="status" className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 text-sm">
                <p className="font-semibold">판정: {outcomes[preview.status]}</p>
                {preview.status === 'review' && <p className="mt-1">자료가 부족하거나 표현이 달라 확인이 필요합니다. 잘못된 정보라는 뜻은 아닙니다.</p>}
                {Object.entries(preview.checks).map(([key,value]) => <p key={key}>{labels[key] || key}: {outcomes[value]}</p>)}
            </div>}
            <button type="button" disabled={!remoteProfile || Boolean(conflict)} className="rounded-lg bg-blue-600 hover:bg-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500 text-white px-4 py-2 disabled:opacity-50" onClick={() => submit(true)}>{busy ? '처리 중…' : '수집 조건 저장'}</button>
        </fieldset>}
        <button type="button" disabled={busy} onClick={onClose} className="text-sm underline disabled:opacity-50">편집 닫기 · 입력 유지</button>
        {settingsDrafts.dirty(draftKey) && <div className="text-sm">
            {!discardRequest ? <button type="button" disabled={busy || settingsDrafts.isWriting()} onClick={() => setDiscardRequest({[draftKey]:settingsDrafts.capture()[draftKey]})} className="underline">미저장 변경 버리기…</button>
                : <><p>이 조건의 미저장 변경을 버리고 저장된 내용으로 돌아갈까요?</p>
                    <button type="button" onClick={() => setDiscardRequest(null)} className="mr-4 underline">계속 편집</button>
                    <button type="button" disabled={busy || settingsDrafts.isWriting()} onClick={() => {
                        settingsDrafts.discard(discardRequest);
                        if (remoteProfile) settingsDrafts.hydrate({[draftKey]:profileDraftValue(remoteProfile)});
                        setDiscardRequest(null);
                    }} className="underline">변경 버리기</button></>}
        </div>}
    </div>;
}

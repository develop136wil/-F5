import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { welfareApi } from '../api/welfare';
import { getErrorMessage } from '../utils/error';
import NewsProfileEditor from './NewsProfileEditor';
import NewsConditionEditor from './NewsConditionEditor';
import NewsConditionWizard from './NewsConditionWizard';
import NewsProfileResults from './NewsProfileResults';
import NewsProfileRunPanel from './NewsProfileRunPanel';
import { useSettingsDirtyNames } from '../hooks/useSettingsDraft';
import { profileDraftKey } from '../utils/profileDraft';
import { profileResults } from '../utils/profileResults';
import { settingsDrafts } from '../utils/settingsDrafts';
import { toast } from 'sonner';
import {Link} from 'react-router-dom';
import {ChevronDown, Plus} from 'lucide-react';
import NewsConditionCardSummary, {conditionCardClass} from './NewsConditionCardSummary';

export default function NewsProfilesPanel({ embedded = false }) {
    const dirtyNames = useSettingsDirtyNames();
    useSyncExternalStore(profileResults.subscribe, profileResults.getVersion, profileResults.getVersion);
    const [items, setItems] = useState([]);
    const [busy, setBusy] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [editing, setEditing] = useState(null);
    const [advancedEditing, setAdvancedEditing] = useState(false);
    const [creating, setCreating] = useState(null);
    const [results, setResults] = useState(null);
    const [running, setRunning] = useState(null);
    const [taxonomy, setTaxonomy] = useState(null);
    const [taxonomyError, setTaxonomyError] = useState('');
    const alive = useRef(false);
    const pending = useRef(false);
    const listRequest = useRef(0);
    useEffect(() => {
        alive.current = true;
        let cancelled = false;
        const request = ++listRequest.current;
        welfareApi.getNewsProfiles().then(data => {
            if (!cancelled && request === listRequest.current) setItems((data.items || []).filter(item=>item.profile_id!=='default-news'));
        }).catch(e => { if (!cancelled) setError(getErrorMessage(e)); })
            .finally(() => { if (!cancelled) setBusy(false); });
        welfareApi.getNewsProfileTaxonomy().then(data => {
            if (!cancelled) setTaxonomy(data);
        }).catch(() => { if (!cancelled) setTaxonomyError('대상 목록을 불러오지 못했습니다.'); });
        return () => { cancelled = true; alive.current = false; };
    }, []);
    async function refreshAfterSave() {
        const request = ++listRequest.current;
        try {
            const data = await welfareApi.getNewsProfiles();
            if (alive.current && request === listRequest.current) setItems((data.items || []).filter(item=>item.profile_id!=='default-news'));
        } catch (e) {
            if (!alive.current || request !== listRequest.current) return;
            const message = `저장은 완료했지만 목록을 새로 읽지 못했습니다. 화면을 다시 열어주세요. ${getErrorMessage(e)}`;
            toast.error(message);
            if (alive.current) setError(message);
        }
    }
    async function action(fn, openEditor = false) {
        if (pending.current) return;
        pending.current = true;
        setBusy(true); setError(''); setNotice('');
        try {
            const result = await settingsDrafts.runWrite(fn);
            const message = '수집 조건을 저장했습니다. 현재 수집에는 적용되지 않습니다.';
            toast.success(message);
            if (alive.current) { setNotice(message); if (openEditor && result?.profile_id) setEditing(result.profile_id); }
            await refreshAfterSave();
        } catch (e) {
            toast.error(getErrorMessage(e));
            if (alive.current) setError(getErrorMessage(e));
        } finally {
            pending.current = false;
            if (alive.current) setBusy(false);
        }
    }
    return <section aria-labelledby="news-profiles-title" aria-busy={busy}
        className={`text-gray-800 dark:text-gray-100 ${embedded ? 'space-y-4' : 'rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1e2128] p-5 space-y-4'}`}>
        <div>
            <h2 id="news-profiles-title" className={embedded ? 'sr-only' : 'text-lg font-semibold text-gray-800 dark:text-gray-100'}>저장한 다른 조건</h2>
            <p className="text-sm text-gray-500 mt-1">필요할 때 새 소식에서 선택해 탐색할 조건입니다. 기본 수집 조건과 예약은 바뀌지 않습니다.</p>
        </div>
        {error && <p role="alert" className="text-sm text-red-600 break-words">{error}</p>}
        {notice && <p role="status" className="text-sm text-green-700 dark:text-green-400">{notice}</p>}
        {creating && <NewsConditionWizard preset={creating} onClose={()=>setCreating(null)} onSave={async value=>{
            const sites=await welfareApi.getSites();
            await settingsDrafts.runWrite(()=>welfareApi.createNewsProfile({...value,site_ids:sites.map(site=>String(site.id))}));
            if(alive.current){setCreating(null);setNotice('수집 조건을 저장했습니다. 새 소식에서 선택하여 실행하세요.');await refreshAfterSave();}
        }}/>}
        {!editing && !creating && <div className="space-y-2">
            <button disabled={busy || settingsDrafts.isWriting()} type="button" onClick={() => setCreating({})}
                className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 dark:border-gray-600 dark:text-gray-200 disabled:opacity-50">
                <Plus aria-hidden="true" className="h-4 w-4" />새 조건 만들기
            </button>
            {taxonomyError && <p role="status" className="text-sm text-amber-700">{taxonomyError} 화면을 다시 열어주세요. 새 조건 만들기는 사용할 수 있습니다.</p>}
        </div>}
        {!editing && !creating && <ul aria-label="보관한 수집 조건" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map(item => profileResults.latest(item)).map(p => <li key={p.profile_id} className={`${conditionCardClass} flex flex-col gap-3`}>
                <div className="min-w-0 break-words [overflow-wrap:anywhere] text-sm text-gray-800 dark:text-gray-100">
                    <NewsConditionCardSummary profile={p}/>
                    {p.protected && <span className="ml-2 text-xs text-gray-500">당시 설정의 복사본</span>}
                    {dirtyNames.includes(profileDraftKey(p.profile_id)) && <span className="ml-2 text-amber-700">미저장 초안 있음</span>}
                    {profileResults.outcome(p.profile_id) && <p role="status" className="mt-1 text-xs text-gray-600 dark:text-gray-300">마지막 저장 결과: {profileResults.outcome(p.profile_id).message}</p>}
                </div>
                <div role="group" aria-label={`${p.name} 관리`} className="mt-auto flex flex-wrap items-start gap-x-3 gap-y-2 border-t border-gray-100 pt-3 dark:border-gray-700">
                    {!p.protected && <button type="button" disabled={busy || Boolean(editing)} onClick={() => {setAdvancedEditing(false);setEditing(p.profile_id);}}
                        className="min-h-9 shrink-0 whitespace-nowrap rounded-lg border border-gray-200 px-3 py-1.5 text-sm dark:border-gray-600 disabled:opacity-50">조건 수정</button>}
                    {!embedded && !p.protected && p.execution_enabled && <button type="button" disabled={busy} onClick={() => setRunning(p)}
                        className="min-h-9 shrink-0 whitespace-nowrap rounded-lg border border-indigo-200 px-3 py-1.5 text-sm text-indigo-700 dark:border-indigo-800 dark:text-indigo-300 disabled:opacity-50">수집 진행</button>}
                    <button type="button" disabled={busy} onClick={() => setResults(p.profile_id)} className="min-h-9 whitespace-nowrap rounded-lg border border-gray-200 px-3 py-1.5 text-sm dark:border-gray-600 disabled:opacity-50">분석 기록 보기</button>
                    {!embedded && <details className="min-w-0 basis-full sm:basis-auto">
                        <summary className="inline-flex min-h-9 cursor-pointer list-none items-center gap-1 rounded-lg px-1 text-sm text-gray-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">더 보기 <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" /></summary>
                        <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
                            {!p.protected && <button type="button" disabled={busy} onClick={()=>{setAdvancedEditing(true);setEditing(p.profile_id);}} className="min-h-9 whitespace-nowrap text-sm text-gray-600 underline underline-offset-4 dark:text-gray-300 disabled:opacity-50">추가 설정</button>}
                            <button type="button" disabled={busy || Boolean(editing)} onClick={() => action(() => welfareApi.cloneNewsProfile(p.profile_id, `${p.name.slice(0, 85)} 복사본`))}
                                className="min-h-9 whitespace-nowrap text-sm text-gray-600 underline underline-offset-4 dark:text-gray-300 disabled:opacity-50">조건 복사</button>
                            <button type="button" disabled={busy} onClick={() => setResults(p.profile_id)}
                                className="min-h-9 whitespace-nowrap text-sm text-gray-600 underline underline-offset-4 dark:text-gray-300 disabled:opacity-50">분석 기록</button>
                        </div>
                    </details>}
                </div>
            </li>)}
        </ul>}
        {!busy && !items.length && <p className="ui-description">보관한 별도 조건이 없습니다. 현재 수집 조건만으로 새 소식을 찾을 수 있습니다.</p>}
        {results && <NewsProfileResults key={results} profileId={results} onClose={() => setResults(null)} />}
        {running && <NewsProfileRunPanel key={running.profile_id} profile={running} onClose={() => setRunning(null)} />}
        {editing && React.createElement(advancedEditing ? NewsProfileEditor : NewsConditionEditor, {key:editing,profileId:editing,initialTaxonomy:taxonomy,onClose:()=>setEditing(null),onSaved:()=>{setEditing(null);refreshAfterSave();}})}
        {!embedded&&!editing&&!creating&&<Link to="/notion" className="inline-block text-sm text-indigo-600 underline underline-offset-4 dark:text-indigo-300">새 소식에서 별도 조건 선택</Link>}
    </section>;
}

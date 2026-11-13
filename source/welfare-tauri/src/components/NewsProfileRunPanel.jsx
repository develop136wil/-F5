import React, { useEffect, useRef, useState } from 'react';
import { welfareApi } from '../api/welfare';
import { getErrorMessage } from '../utils/error';
import {RefreshCw} from 'lucide-react';
import NewsTaskProgress from './NewsTaskProgress';
import {useSettingsDirtyNames} from '../hooks/useSettingsDraft';
import {profileDraftKey} from '../utils/profileDraft';
import {useAiStatus} from '../hooks/useAiStatus';
import {ageHint} from '../utils/newsProfileForm';
import {publicationLabel} from '../utils/newsPublication';
import {useQueryClient} from '@tanstack/react-query';
import {queryKeys} from '../api/queryKeys';
import {splitHistory, latestTerminalWorkflow, canClearHistory, historyDate, historyConfirmation} from '../utils/newsHistory';
import {newsTaskProgress} from '../utils/newsTaskProgress';
import {readNewsWorkflow,readNewsWorkflowHistory,readNewsWorkflowOptions} from '../utils/newsResponse';
import {readHiddenTasks, writeHiddenTasks, hideTaskConfirmation} from '../utils/newsTaskVisibility';
import {blocksNewWorkflow, canResumeWorkflow, canStartAdditionalCollection, analysisReadinessText, workflowStatusText, workflowRevisionText, workflowNotice, sourceProgressText} from '../utils/newsWorkflowState';

export default function NewsProfileRunPanel({ profile, onClose, onResults, onRecords, actionBusy=false, primaryLabel, conditionActions }) {
    const queryClient=useQueryClient();
    const [options, setOptions] = useState(null), [items, setItems] = useState([]);
    const [busy, setBusy] = useState(true), [error, setError] = useState('');
    const [pollError, setPollError] = useState('');
    const [confirming,setConfirming]=useState(false);
    const confirmedRevision=useRef(null);
    const [historyOpen,setHistoryOpen]=useState(false);
    const [historyMessage,setHistoryMessage]=useState('');
    const [hiddenIds,setHiddenIds]=useState(()=>readHiddenTasks(profile.profile_id));
    const [hiddenOpen,setHiddenOpen]=useState(false);
    const [refreshing,setRefreshing]=useState(false);
    const dirtyNames=useSettingsDirtyNames();
    const ai=useAiStatus();
    const active = items.find(item => item.active || item.state === 'running' || item.state === 'ready');
    const unfinished=items.some(item=>blocksNewWorkflow(item,options?.selection?.revision));
    const additionalCollection=canStartAdditionalCollection(items,options);
    const analysisNotice=analysisReadinessText(options,ai);
    const needsEnd=items.some(item=>item.state==='waiting'&&item.can_resume===false&&blocksNewWorkflow(item,options?.selection?.revision));
    const staleSelection=Number.isInteger(profile.revision)&&options?.selection?.revision<profile.revision;
    const blocked=actionBusy?'다른 수집·저장 처리가 끝난 뒤 시작할 수 있습니다.'
        :pollError?'수집 상태를 다시 확인한 뒤 시작해주세요.'
        :staleSelection?'변경된 수집 조건을 확인하고 있습니다.'
        :dirtyNames.includes(profileDraftKey(profile.profile_id))||(profile.profile_id==='default-news'&&dirtyNames.includes('defaultNewsConditionDraft'))?'설정에서 미저장 조건을 먼저 저장하거나 변경을 취소해주세요.':'';
    const mounted = useRef(false), pending = useRef(false), generation = useRef(0);
    const observedProfile=useRef(null);
    async function load() {
        const request = ++generation.current;
        const [rawOptions, rawHistory] = await Promise.all([
            welfareApi.getNewsWorkflowOptions(profile.profile_id), welfareApi.getNewsWorkflows(profile.profile_id)]);
        const nextOptions=readNewsWorkflowOptions(rawOptions),history=readNewsWorkflowHistory(rawHistory);
        if (mounted.current && request === generation.current) { setOptions(nextOptions); setItems(history.items); setError(''); setPollError(''); }
    }
    useEffect(() => {
        mounted.current = true;
        observedProfile.current=`${profile.profile_id}:${profile.revision}`;
        setItems([]); setOptions(null); setHistoryOpen(false); setHistoryMessage(''); setBusy(true); setError('');
        setHiddenIds(readHiddenTasks(profile.profile_id)); setHiddenOpen(false);
        load().catch(e => { if (mounted.current) setError(getErrorMessage(e)); })
            .finally(() => { if (mounted.current) setBusy(false); });
        return () => { mounted.current = false; generation.current += 1; };
    }, [profile.profile_id]);
    useEffect(() => {
        const identity=`${profile.profile_id}:${profile.revision}`;
        if(observedProfile.current===identity)return;
        observedProfile.current=identity;
        setConfirming(false);setOptions(null);setBusy(true);setError('');
        load().catch(e=>{if(mounted.current)setError(getErrorMessage(e));})
            .finally(()=>{if(mounted.current)setBusy(false);});
    }, [profile.profile_id,profile.revision]);
    useEffect(() => {
        if(active||busy)return;
        let stopped=false,reading=false;
        const refresh=async()=>{
            if(stopped||reading||pending.current||document.visibilityState==='hidden')return;
            reading=true;
            const request=generation.current+1;
            try {await load();}
            catch(e){if(!stopped&&mounted.current&&!pending.current&&request===generation.current){setPollError(getErrorMessage(e));setError(getErrorMessage(e));}}
            finally{reading=false;}
        };
        // Registration may finish after Sites unmounts. Refresh idle readiness
        // without starting work, touching settings, or polling active jobs twice.
        const timer=setInterval(refresh,10000);
        const unsubscribe=queryClient.getQueryCache().subscribe(event=>{
            if(event.type==='updated'&&event.action?.type==='invalidate'
                &&event.query.queryKey[0]===queryKeys.sites[0])refresh();
        });
        return()=>{stopped=true;clearInterval(timer);unsubscribe();};
    }, [profile.profile_id,profile.revision,Boolean(active),busy,queryClient]);
    useEffect(() => {
        if (!active) return;
        const controller = new AbortController();
        let timer;
        const poll = async () => {
            try {
                const request = generation.current;
                const response = await welfareApi.getNewsWorkflow(active.workflow_id, controller.signal);
                if (!mounted.current || controller.signal.aborted) return;
                if (request !== generation.current || pending.current) {
                    timer = setTimeout(poll, 2000);
                    return;
                }
                const next=readNewsWorkflow(response,active.workflow_id);
                setItems(current => current.map(item => item.workflow_id === next.workflow_id ? next : item));
                setPollError('');
                if (next.active || next.state === 'running' || next.state === 'ready') {
                    timer = setTimeout(poll, 2000);
                } else {
                    queryClient.invalidateQueries({queryKey:queryKeys.newsFeed});
                    queryClient.invalidateQueries({queryKey:['global-news-task-status']});
                    await load();
                }
            } catch (e) {
                if (mounted.current && !controller.signal.aborted && e?.name !== 'AbortError') {
                    setPollError(getErrorMessage(e));
                    timer = setTimeout(poll, 4000);
                }
            }
        };
        timer = setTimeout(poll, 2000);
        return () => { clearTimeout(timer); controller.abort(); };
    }, [active?.workflow_id]);
    async function act(fn) {
        if (pending.current) return;
        const actionGeneration = ++generation.current;
        pending.current = true; setBusy(true); setError('');
        try {
            await fn();
            queryClient.invalidateQueries({queryKey:['global-news-task-status']});
            if (!mounted.current || generation.current !== actionGeneration) return;
            setConfirming(false);
            try { await load(); }
            catch (e) {
                if (mounted.current) {
                    // Keep the last condition visible, but block another start until a valid refresh.
                    setPollError(getErrorMessage(e));
                    setError(`최신 작업 상태를 읽지 못했습니다. 상태 새로고침으로 확인해주세요. ${getErrorMessage(e)}`);
                }
            }
        }
        catch (e) { if (mounted.current && generation.current === actionGeneration) setError(getErrorMessage(e)); }
        finally { pending.current = false; if (mounted.current) setBusy(false); }
    }
    function confirmStart(kind) {
        confirmedRevision.current=options?.selection?.revision;
        setConfirming(kind);
    }
    async function startConfirmed() {
        const expected=confirmedRevision.current;
        const request=generation.current;
        const fresh=readNewsWorkflowOptions(await welfareApi.getNewsWorkflowOptions(profile.profile_id));
        if(!mounted.current||request!==generation.current)return;
        setOptions(fresh);
        if(fresh.selection?.revision!==expected){
            setConfirming(false);
            throw Error('수집 조건이 변경되었습니다. 위의 최신 범위와 기간을 확인한 뒤 다시 시작해주세요.');
        }
        if(!fresh.enabled)throw Error(fresh.reason||'수집 준비 상태가 변경되었습니다. 조건과 사이트를 확인해주세요.');
        return confirming==='new'?welfareApi.startNewsWorkflow(profile.profile_id,expected,{newCollection:true})
            :welfareApi.startNewsWorkflow(profile.profile_id,expected);
    }
    const button = 'rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm disabled:opacity-50';
    const hidden = items.filter(item=>hiddenIds.includes(item.workflow_id));
    const {foreground, history} = splitHistory(items.filter(item=>!hiddenIds.includes(item.workflow_id)),options?.selection?.revision);
    const latestTerminal=latestTerminalWorkflow(items,options?.selection?.revision);
    const terminal=latestTerminal&&!hiddenIds.includes(latestTerminal.workflow_id)?latestTerminal:null;
    const terminalProgress=terminal?newsTaskProgress(terminal):null;
    const setTaskHidden = (id,hide) => {
        if (hide && !window.confirm(hideTaskConfirmation)) return;
        const next = hide ? [...new Set([...hiddenIds,id])].slice(-100) : hiddenIds.filter(value=>value!==id);
        setHiddenIds(next);
        const saved=writeHiddenTasks(profile.profile_id,next);
        setHistoryMessage(saved ? '' : '숨김 설정을 저장하지 못했습니다. 이번 화면에서만 적용됩니다.');
    };
    const clearHistory = workflowIds => {
        if (busy || actionBusy || active || !window.confirm(historyConfirmation)) return;
        setHistoryMessage('');
        act(async () => {
            const request = generation.current;
            const result = await welfareApi.clearNewsWorkflowHistory(profile.profile_id,workflowIds);
            if (mounted.current && request === generation.current) setHistoryMessage(`${result.cleared}개의 완료 이력을 목록에서 정리했습니다. 소식은 그대로 보존됩니다.`);
        });
    };
    const renderDetails = (item, showProgress=true) => <>
            {showProgress && <NewsTaskProgress task={item} unavailable={Boolean(pollError)&&item.workflow_id===active?.workflow_id} showNotice={false}/>}
            {workflowNotice(item) && <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">{workflowNotice(item)}</p>}
            {item.progress?.schema_version!==1 && Number(item.summary?.analysis?.remaining) > 0 && <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">남은 분석 {item.summary.analysis.remaining}건</p>}
            {sourceProgressText(item)&&<p className="mt-1 text-xs font-medium text-blue-700 dark:text-blue-300">{sourceProgressText(item)}</p>}
            {item.revision!==options?.selection?.revision&&<p className="mt-1 text-xs text-amber-700 dark:text-amber-300">{workflowRevisionText(item,options?.selection?.revision)}</p>}
            <details className="mt-1 text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">처리 내역</summary>
            {item.revision===options?.selection?.revision&&<p className="mt-1">{workflowRevisionText(item,options?.selection?.revision)}</p>}
            {Number(item.summary?.collection?.attachments_analyzed) > 0 && <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
                첨부파일 {item.summary.collection.attachments_analyzed}개에서 내용을 읽었습니다.
                {Number(item.summary.collection.attachments_reused) > 0 && <span className="block mt-1">
                    이 중 {item.summary.collection.attachments_reused}개는 변경이 없어, 이전에 읽은 내용을 다시 사용했습니다.
                </span>}
            </p>}
            {Number(item.summary?.collection?.attachment_review) > 0 && <p className="mt-2 text-xs text-amber-800 dark:text-amber-200">
                첨부파일 {item.summary.collection.attachment_review}개는 직접 확인이 필요합니다. 한글·PDF·이미지는 먼저 PC에서 읽으며, 이미지 인식 결과·암호 문서·읽지 못한 부분은 원문을 확인해주세요.
            </p>}
            {Number(item.summary?.collection?.content_failed) > 0 && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                본문 근거를 확인하지 못한 게시물 {item.summary.collection.content_failed}건이 있습니다. 결과를 확정하기 전에 원문을 확인해주세요.
            </p>}
            {Number(item.summary?.collection?.skipped_candidates || item.summary?.collection?.sources?.sites?.skipped_candidates) > 0 && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                원문 링크나 제목을 확인할 수 없어 수집하지 못한 자료가 있습니다. 수집 사이트 설정과 기관 게시판을 확인해주세요. 새 소식이 없다는 뜻은 아니며, 같은 작업에서 자동으로 반복 요청하지 않습니다.
            </p>}
            {Number(item.summary?.collection?.vision_review_sites || item.summary?.collection?.sources?.sites?.vision_review_sites) > 0 && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                {Number(item.summary?.collection?.vision_review_sites || item.summary?.collection?.sources?.sites?.vision_review_sites)}개 사이트에서 화면 이미지의 추가 분석은 직접 확인이 필요합니다. 개인정보 확인 전에는 화면을 AI로 보내지 않습니다. 찾은 링크는 보존하며, 새 소식이 없다는 뜻은 아닙니다. 기관 게시판과 수집 사이트 설정을 확인해주세요.
            </p>}
            {(item.summary?.collection?.site_issues || item.summary?.collection?.sources?.sites?.site_issues || []).length > 0 && <div className="mt-2 text-amber-800 dark:text-amber-200">
                <p>아래 사이트는 일부 자료를 확인하지 못했습니다. 다른 사이트의 수집 결과는 보존됩니다.</p>
                <ul className="mt-1 list-disc space-y-1 pl-5">{(item.summary?.collection?.site_issues || item.summary?.collection?.sources?.sites?.site_issues).map(issue=><li key={issue.site_id}>
                    {issue.site_name || '등록 기관 사이트'} · {issue.issue_code==='site_partial'?'일부만 확인':'목록 확인 필요'}
                </li>)}</ul>
                <p className="mt-1">수집 사이트 등록·관리에서 게시판 주소와 설정을 확인해주세요. 화면 이미지는 AI로 자동 전송하지 않습니다.</p>
            </div>}
            </details>
            {(item.active || item.state === 'running' || item.state === 'ready') && <button className={`${button} mt-2`} disabled={busy}
                onClick={() => act(() => welfareApi.cancelNewsWorkflow(item.workflow_id))}>이 작업 중지</button>}
            {!item.active && item.resume_block_reason !== 'news_source_scope_changed' && (item.state === 'waiting' || canResumeWorkflow(item)) && <div className="mt-2 flex flex-wrap gap-2">
                {canResumeWorkflow(item) && <button className={button} disabled={busy||Boolean(blocked)||(item.stage!=='collecting'&&Boolean(analysisNotice))}
                    onClick={() => act(() => welfareApi.resumeNewsWorkflow(item.workflow_id))}>{item.state === 'failed' ? '문제 해결 후 이어서 진행' : '이어서 진행'}</button>}
                <button className={button} disabled={busy}
                    onClick={() => act(() => welfareApi.cancelNewsWorkflow(item.workflow_id))}>이 작업 종료</button>
            </div>}
            {onResults&&<button type="button" className={`${button} mt-2`} onClick={()=>onResults(item.run_id)}>찾은 소식 보기</button>}
    </>;
    return <section aria-label="대상별 수집 실행" aria-busy={busy} className={primaryLabel?'space-y-3 rounded-2xl border border-gray-100 bg-white p-5 dark:border-gray-700 dark:bg-[#1e2128]':'rounded-xl border border-blue-200 dark:border-blue-900 p-4 space-y-3'}>
        <div className="space-y-4">
        <div className="min-w-0 space-y-3">
        {primaryLabel?<h2 className="text-base font-semibold leading-6 text-gray-800 dark:text-gray-100">수집 범위</h2>:<h3 className="font-semibold text-gray-800 dark:text-gray-100">{`${options?.selection?.name||profile.name} 수집`}</h3>}
        {options?.selection&&<dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 rounded-xl bg-gray-50 p-4 text-sm text-gray-800 dark:bg-gray-900/40 dark:text-gray-200">
            <dt className="text-gray-500 dark:text-gray-400">지역</dt><dd className="min-w-0 break-words font-medium">{options.selection.region||'지역 제한 없음'}</dd>
            <dt className="text-gray-500 dark:text-gray-400">대상</dt><dd className="min-w-0 break-words">{[...options.selection.life_cycles,...options.selection.characteristics].join(' · ')||'대상 분류 제한 없음'}</dd>
            <dt className="text-gray-500 dark:text-gray-400">나이</dt><dd className="min-w-0 break-words">{options.selection.min_age_months==null&&options.selection.max_age_months==null?'제한 없음':`${options.selection.min_age_months==null?'최소 나이 제한 없음':ageHint(String(options.selection.min_age_months))} ~ ${options.selection.max_age_months==null?'최대 나이 제한 없음':ageHint(String(options.selection.max_age_months))}`}</dd>
            <dt className="text-gray-500 dark:text-gray-400">확인할 곳</dt><dd className="min-w-0 break-words">등록 기관 사이트</dd>
            <dt className="text-gray-500 dark:text-gray-400">게시 기간</dt><dd className="min-w-0 break-words">{publicationLabel(options.selection)} · 게시일 기준</dd>
        </dl>}
        {error && <p role="alert" className="text-sm text-red-600 break-words">{error}</p>}
        {options && !options.enabled && <div role="status" className="rounded-lg bg-amber-50 dark:bg-amber-950/30 p-3 text-sm text-amber-900 dark:text-amber-100">
            <p className="font-medium">{options.reason_code === 'QUALITY_VALIDATION_REQUIRED' ? '조건 저장 완료 · 검색 품질 검증 대기' : '아직 실행 준비가 끝나지 않았습니다.'}</p><p className="mt-1">{options.reason}</p>
        </div>}
        {blocked&&<p role="status" className="text-sm text-amber-800 dark:text-amber-200">{blocked}</p>}
        {analysisNotice&&<p role="status" className="text-sm text-gray-600 dark:text-gray-300"><span className="font-medium">AI 분석 준비: </span>{analysisNotice}</p>}
        {conditionActions&&<div className="flex flex-wrap items-center gap-2 pt-1">{conditionActions}</div>}
        </div>
        <div className={`flex min-w-0 items-start gap-2 ${primaryLabel ? 'border-t border-gray-100 pt-4 dark:border-gray-700' : ''}`}>
        {!confirming?<button type="button" className={`min-h-14 min-w-0 flex-1 rounded-xl text-white px-6 py-4 text-base font-semibold shadow-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-50 ${primaryLabel ? 'sm:w-1/2 sm:flex-none bg-teal-700 hover:bg-teal-800 focus-visible:outline-teal-700' : 'bg-indigo-600 hover:bg-indigo-700 focus-visible:outline-indigo-600'}`}
            disabled={busy || !options?.enabled || !options?.selection || (unfinished&&!additionalCollection) || Boolean(blocked)} onClick={() => confirmStart(additionalCollection?'new':'initial')}>
            {active ? (active.stage==='analyzing'?'수집한 정보를 분석하는 중…':'새 소식을 찾는 중…') : additionalCollection ? '새 원문 수집' : unfinished ? (needsEnd ? '기존 작업의 결과를 확인하고 종료해주세요' : '기존 작업을 이어서 진행하거나 종료해주세요') : (primaryLabel || '이 조건으로 수집하기')}
        </button>:<div className="rounded-xl bg-blue-50 p-3 space-y-3 dark:bg-blue-950/30">
            {confirming==='new'&&<p className="text-sm font-medium">이전 회차의 원문과 분석 대기는 그대로 보관하고, 위 조건으로 새 회차의 원문을 수집합니다. 이전 분석을 다시 시작하는 동작은 아닙니다.</p>}
            <p className="text-sm">위 조건과 게시 기간으로 등록한 기관 게시판에서 새 소식을 찾을까요? 관련 소식은 아래 ‘찾은 소식’에 모이며, 미분석 자료는 ‘처리 기록’에서 확인합니다. 한글·PDF·이미지는 먼저 PC에서 읽고 AI 분석은 Google의 사용 한도를 따릅니다. Notion에는 자동 등록하지 않습니다.</p>
            <div className="flex flex-wrap gap-3"><button type="button" className="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50"
                disabled={busy||!options?.enabled||!options?.selection||(unfinished&&!(confirming==='new'&&additionalCollection))||Boolean(blocked)} onClick={()=>act(startConfirmed)}>{confirming==='new'?'확인하고 새 원문 수집':'확인하고 수집 시작'}</button>
                <button type="button" className={button} disabled={busy} onClick={()=>setConfirming(false)}>취소</button></div>
        </div>}
        <span className="group relative ml-auto shrink-0">
            <button type="button" aria-label="상태 새로고침" disabled={busy||actionBusy} onClick={async()=>{setRefreshing(true);try{await act(async()=>{});}finally{if(mounted.current)setRefreshing(false);}}} className="flex h-14 w-14 items-center justify-center rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-40 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-800">
                <RefreshCw aria-hidden="true" className={`h-4 w-4 ${refreshing?'animate-spin motion-reduce:animate-none':''}`}/>
            </button>
            <span role="tooltip" className="pointer-events-none absolute right-0 top-full z-20 mt-1 whitespace-nowrap rounded-md bg-gray-800 px-2 py-1 text-xs text-white opacity-0 group-hover:opacity-100 group-focus-within:opacity-100">상태 새로고침</span>
        </span>
        </div>
        </div>
        {historyMessage&&<p role="status" className="text-xs text-gray-500">{historyMessage}</p>}
        <ul className="space-y-2">{foreground.map(item=><li key={item.workflow_id} className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 text-sm text-gray-800 dark:text-gray-100"><div className="mb-1 flex justify-end"><button type="button" className="text-xs text-gray-500 underline" onClick={()=>setTaskHidden(item.workflow_id,true)}>숨기기</button></div>{renderDetails(item)}</li>)}</ul>
        {terminal&&<div aria-label="최근 수집 결과" className="space-y-2 rounded-lg bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-800 dark:text-gray-100">
            <p role="status" className="font-semibold">최근 수집 · {workflowStatusText(terminal)}</p>
            {terminal.progress?.schema_version===1&&<p className="text-xs text-gray-600 dark:text-gray-300">{[terminalProgress.amount,...terminalProgress.lines].join(' · ')}</p>}
            <p className="text-xs leading-5 text-gray-600 dark:text-gray-300">{workflowNotice(terminal)||'처리 결과는 찾은 소식과 처리 기록에서 확인할 수 있습니다.'}</p>
            <div className="flex flex-wrap gap-2">
                {onResults&&<button type="button" className={button} onClick={()=>onResults(terminal.run_id)}>찾은 소식 확인</button>}
                {onRecords&&<button type="button" className={button} onClick={()=>onRecords(Number(terminal.summary?.analysis?.review_required_count||terminal.progress?.analysis_review_required)>0?'review':terminal.state==='complete'?'records':'processing')}>처리 기록 보기</button>}
            </div>
        </div>}
        {hidden.length>0&&<details open={hiddenOpen} onToggle={event=>{if(event.target===event.currentTarget)setHiddenOpen(event.currentTarget.open);}} className="text-sm text-gray-500">
            <summary className="w-fit cursor-pointer">숨긴 작업 {hidden.length}건 · 보기</summary>
            {hiddenOpen&&<ul className="mt-2 divide-y divide-gray-100 dark:divide-gray-700">{hidden.map(item=><li key={item.workflow_id} className="py-2">
                <div className="flex flex-wrap items-center gap-2"><span>{workflowStatusText(item)}</span><button type="button" className="ml-auto text-xs underline" onClick={()=>setTaskHidden(item.workflow_id,false)}>다시 표시</button></div>
                <details className="mt-1 text-xs"><summary className="w-fit cursor-pointer">작업 보기</summary><div className="mt-2">{renderDetails(item,false)}</div></details>
            </li>)}</ul>}
        </details>}
        {history.length>0&&<details open={historyOpen} onToggle={event=>{if(event.target===event.currentTarget)setHistoryOpen(event.currentTarget.open);}} className="border-t border-gray-100 pt-2 text-sm dark:border-gray-700">
            <summary className="w-fit cursor-pointer py-1 text-gray-500">수집 이력</summary>
            {historyOpen&&<div className="mt-2 space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-gray-500">최근 이력만 표시합니다. 소식은 이력을 정리해도 유지됩니다.</p>
                    <button type="button" className={button} disabled={busy||actionBusy||Boolean(active)||!history.some(canClearHistory)} onClick={()=>clearHistory()}>완료 이력 정리</button>
                </div>
                <ul className="divide-y divide-gray-100 dark:divide-gray-700">{history.map(item=><li key={item.workflow_id} className="py-2 text-gray-700 dark:text-gray-200">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <span className="text-xs text-gray-500">{historyDate(item.updated_at)}</span>
                        <span className="text-sm">{workflowStatusText(item)}</span>
                        {canClearHistory(item)&&<button type="button" className="ml-auto text-xs text-gray-500 underline disabled:opacity-50" disabled={busy||actionBusy||Boolean(active)} onClick={()=>clearHistory([item.workflow_id])}>목록에서 지우기</button>}
                        {!canClearHistory(item)&&<button type="button" className="ml-auto text-xs text-gray-500 underline" onClick={()=>setTaskHidden(item.workflow_id,true)}>숨기기</button>}
                    </div>
                    <details className="mt-1 text-xs"><summary className="w-fit cursor-pointer text-gray-500">상세 보기</summary><div className="mt-2">{renderDetails(item,false)}</div></details>
                </li>)}</ul>
            </div>}
        </details>}
        {!primaryLabel && !items.length && !busy && <p className="text-sm text-gray-500">아직 실행 기록이 없습니다.</p>}
        {onClose&&<button className={button} disabled={busy} onClick={onClose}>실행 화면 닫기</button>}
    </section>;
}

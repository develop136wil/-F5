import React from 'react';
import NewsProfileRunPanel from './NewsProfileRunPanel';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Square, Loader2 } from 'lucide-react';
import { useScrape } from '../context/ScrapeContext';
import { useAiStatus } from '../hooks/useAiStatus';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { splitAgeMonths } from '../utils/ageMonths';
import {readNewsDefaultCondition} from '../utils/newsResponse';

export default function NewsCollectionPanel({ actionBusy = false, conditionActions, onResults, onRecords }) {
    const legacy=useScrape();
    const condition=useQuery({queryKey:['defaultNewsCondition'],queryFn:async()=>readNewsDefaultCondition(await welfareApi.getDefaultNewsCondition())});
    // Creating a condition must not hide the stop/progress controls for a
    // legacy run that was already started under its original settings.
    if(legacy.isScraping||legacy.isStarting)return <LegacyNewsCollectionPanel actionBusy={actionBusy}
        conditionActions={conditionActions} needsSetup={false}/>;
    if(condition.isLoading)return <p role="status">수집 조건을 확인하고 있습니다.</p>;
    if(condition.isError&&!condition.data)return <p role="alert">수집 조건을 확인하지 못했습니다. <button onClick={()=>condition.refetch()} className="underline">다시 확인</button></p>;
    if(condition.data?.profile)return <div className="space-y-4">
        <NewsProfileRunPanel profile={condition.data.profile} actionBusy={actionBusy} onResults={()=>onResults?.(condition.data.profile.profile_id,condition.data.profile.name)} onRecords={onRecords} primaryLabel="새 소식 찾기" conditionActions={<><Link to="/settings?tab=keywords" className="inline-flex min-h-10 items-center rounded-lg border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200">수집 범위·기간 설정</Link>{conditionActions}<Link to="/sites" className="inline-flex min-h-10 items-center rounded-lg border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200">수집 사이트 등록·관리</Link></>} />
        {condition.isError&&<p role="status" className="text-sm text-amber-700 dark:text-amber-300">수집 조건 업데이트를 확인하지 못했습니다. 마지막으로 확인한 조건과 진행 상태를 유지합니다. <button onClick={()=>condition.refetch()} className="underline">다시 확인</button></p>}
    </div>;
    return <LegacyNewsCollectionPanel actionBusy={actionBusy} conditionActions={conditionActions}
        needsSetup={condition.data?.legacy_collection_available!==true}/>;
}

function LegacyNewsCollectionPanel({actionBusy,conditionActions,needsSetup}) {
    const navigate=useNavigate();
    const { isScraping, isStarting, isStopping, progress, statusMessage, aiSavings, completionNotice, startScrape, stopScrape } = useScrape();
    const ai = useAiStatus();
    const config = useQuery({ queryKey: queryKeys.config, queryFn: welfareApi.getConfig });
    const sites = useQuery({ queryKey: queryKeys.sites, queryFn: welfareApi.getSites });
    const backlog = useQuery({ queryKey: queryKeys.aiBacklogStatus, queryFn: welfareApi.getAiBacklogStatus, staleTime: 60000 });
    const age = splitAgeMonths(config.data?.TARGET_AGE_MAX_MONTHS, Number.MAX_SAFE_INTEGER);
    const setupFirst=needsSetup&&!isScraping&&!isStarting;
    const reason = actionBusy ? '저장·등록 처리가 끝난 뒤 시작할 수 있습니다.'
        : isStarting ? '새 소식 찾기를 준비하고 있습니다.'
        : setupFirst ? ''
        : config.isLoading || sites.isLoading || ai.isLoading ? '시작할 수 있는지 확인하고 있습니다.'
        : config.isError || sites.isError || ai.isError ? '연결 상태를 확인하지 못했습니다. 잠시 후 다시 확인해주세요.'
        : !config.data?.is_configured ? '설정 도우미에서 AI 연결을 먼저 준비해주세요.'
        : !sites.data?.length ? '공지사항·모집 게시판을 먼저 등록해주세요.'
        : ai.data?.busy ? `진행 중: ${ai.data.job_label || '다른 AI 작업'}. 완료 후 시작할 수 있습니다.` : '';
    const waiting = backlog.data?.backlog_total || 0;
    const excluded = backlog.data?.permanently_excluded || 0;
    return <section aria-labelledby="news-collection-title" className="space-y-4 rounded-2xl border border-gray-100 bg-white p-5 dark:border-gray-700 dark:bg-[#1e2128]">
        <div className="min-w-0 space-y-3">
            <h2 id="news-collection-title" className="text-base font-semibold leading-6 text-gray-800 dark:text-gray-100">수집 범위</h2>
            {setupFirst?<p className="text-sm leading-6 text-gray-600 dark:text-gray-300">수집 범위·기간을 먼저 저장해주세요. ‘새 소식 찾기’를 누르면 설정으로 이동합니다.</p>:config.data && !config.isError ? <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 rounded-xl bg-gray-50 p-4 text-sm text-gray-800 dark:bg-gray-900/40 dark:text-gray-200">
                <dt className="text-gray-500 dark:text-gray-400">지역</dt><dd className="min-w-0 break-words font-medium">{config.data.TARGET_REGION || '지역 미설정'}</dd>
                <dt className="text-gray-500 dark:text-gray-400">나이</dt><dd className="min-w-0 break-words">{config.data.TARGET_AGE_MAX_MONTHS == null ? '나이 미설정' : `만 ${age.years}세${age.months ? ` ${age.months}개월` : ''}까지`}</dd>
                <dt className="text-gray-500 dark:text-gray-400">확인할 곳</dt><dd className="min-w-0 break-words">등록 기관 사이트</dd>
            </dl> : <p className="text-sm leading-6 text-gray-600 dark:text-gray-300">{config.isError ? '수집 조건을 확인하지 못했습니다.' : '수집 조건을 확인하고 있습니다.'}</p>}
            <div className="flex flex-wrap items-center gap-2 pt-1">
                <Link to="/settings?tab=keywords" className="inline-flex min-h-10 items-center rounded-lg border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200">수집 범위·기간 설정</Link>
                {conditionActions}
                <Link to="/sites" className="inline-flex min-h-10 items-center rounded-lg border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200">수집 사이트 등록·관리</Link>
            </div>
            {!setupFirst && !isScraping && !isStarting && <p className="text-xs leading-5 text-gray-500 dark:text-gray-400">처음 시작한다면 <Link to="/settings?tab=keywords" className="underline underline-offset-2">수집 범위·기간을 확인하고 저장</Link>한 뒤 새 소식을 찾아보세요.</p>}
        </div>
        <div className="flex min-w-0 items-start gap-2 border-t border-gray-100 pt-4 dark:border-gray-700">
            {isScraping ? <button type="button" onClick={stopScrape} disabled={isStopping} className="inline-flex min-h-11 shrink-0 items-center gap-2 rounded-xl border border-red-200 px-4 py-2 text-sm font-semibold text-red-600 disabled:opacity-50">
                {isStopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}{isStopping ? '중지 요청 중…' : '찾기 중지'}
            </button> : <button type="button" onClick={()=>setupFirst?navigate('/settings?tab=keywords'):startScrape()} disabled={Boolean(reason)} title={reason || undefined} className="inline-flex min-h-14 min-w-0 flex-1 items-center justify-center gap-2 rounded-xl bg-teal-700 px-6 py-4 text-base font-semibold text-white shadow-md hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:opacity-50 sm:w-1/2 sm:flex-none">
                {isStarting && <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />}{isStarting ? '시작 요청 중…' : '새 소식 찾기'}
            </button>}
        </div>
        {isScraping ? <div role="status" className="space-y-2 text-sm text-blue-700 dark:text-blue-300">
            <div className="flex items-start justify-between gap-3"><span className="min-w-0 break-words">{statusMessage || '새 소식을 찾는 중입니다.'}</span><span className="shrink-0">{progress}%</span></div>
            <div role="progressbar" aria-label="새 소식 찾기 진행률" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} className="h-2 overflow-hidden rounded-full bg-blue-100 dark:bg-blue-950"><div className="h-full bg-blue-600 transition-all" style={{width: `${progress}%`}} /></div>
            <p className="text-xs text-gray-500">다른 화면으로 이동해도 계속 진행됩니다. 왼쪽 메뉴에서도 중지할 수 있습니다.</p>
        </div> : reason && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">{reason}{config.data && !config.data.is_configured && !config.isError && <Link to="/settings?tab=guide&step=google" className="ml-2 underline">AI 연결하기</Link>}</p>}
        {!isScraping && completionNotice && <p role={completionNotice.tone === 'error' ? 'alert' : 'status'} className="text-sm leading-6 text-amber-700 dark:text-amber-300">{completionNotice.message}</p>}
        {(waiting > 0 || excluded > 0) && <details className="text-sm leading-6 text-gray-600 dark:text-gray-300">
            <summary className="cursor-pointer">AI 분석 대기 {waiting}건{excluded > 0 ? ` · 자동 분석 제외 ${excluded}건` : ''}</summary>
            {waiting > 0 && <p className="text-xs">아직 분석하지 못한 자료입니다. 다음 수집 때 사용 가능한 한도 안에서 이어서 처리합니다.</p>}
            {excluded > 0 && <p className="text-xs">원문 접근 제한 등으로 자동 재시도하지 않는 자료가 있습니다.</p>}
        </details>}
        {aiSavings && <details className="text-sm text-gray-600 dark:text-gray-300">
            <summary className="cursor-pointer">마지막 수집 처리 내역</summary>
            <p className="mt-2 leading-6">AI 분석 {aiSavings.analyzed || 0}건 · 기존 내용 재사용 {aiSavings.cache || 0}건 · 유사 내용 재사용 {aiSavings.semantic || 0}건 · 한도 때문에 보류 {aiSavings.quota_skipped || 0}건</p>
            <p className="mt-1 text-xs">대기 항목 처리 완료 {aiSavings.backlog_completed || 0}건 · 계속 대기 {aiSavings.backlog_deferred || 0}건</p>
        </details>}
    </section>;
}

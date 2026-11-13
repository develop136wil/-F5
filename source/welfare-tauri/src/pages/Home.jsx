import { Skeleton } from '../components/ui/Skeleton';
import HomeWorkspace from '../components/HomeWorkspace';
import { queryCountStatus } from '../utils/queryCountStatus';

import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card';
import {
    AlertTriangle, ExternalLink, CheckCircle2, FileEdit,
    Key, ArrowRight, Sparkles, X, ChevronDown, BookOpen, RefreshCw,
} from 'lucide-react';
import { useScrape } from '../context/ScrapeContext';
import { isRealLink } from '../utils/link';
import { currentVerification } from '../utils/verification';

const SETUP_CARD_COLLAPSED_KEY = 'welfare-f5-home-setup-card-collapsed';

const Home = () => {
    // 수집 상태/제어는 전역 ScrapeContext에서 — 페이지를 떠나도 폴링/완료 처리가 유지됨
    const { isScraping, progress } = useScrape();
    const [welcomeDismissed, setWelcomeDismissed] = useState(() => window.localStorage.getItem('welfare-f5-welcome-seen') === 'true');
    const [setupCardCollapsed, setSetupCardCollapsed] = useState(() => window.localStorage.getItem(SETUP_CARD_COLLAPSED_KEY) === 'true');

    const { data: expiringData } = useQuery({
        queryKey: queryKeys.expiringItems(7),
        queryFn: () => welfareApi.getExpiringItems(7),
        refetchInterval: 60000,
        refetchOnWindowFocus: true,
    });

    // 첫 실행 온보딩 — API 키/사이트/Notion 설정 여부 확인 (Settings 로드와 동일한 신호 재사용)
    const { data: config, isLoading } = useQuery({
        queryKey: queryKeys.config,
        queryFn: welfareApi.getConfig,
    });
    const { data: homeGoogleCheck } = useQuery({
        queryKey: ['tutorialGoogleVerification', config?.revision],
        queryFn: welfareApi.verifyGoogleApiKey,
        enabled: Boolean(config?.is_configured),
        staleTime: 5 * 60 * 1000,
        retry: false,
    });
    const { data: sites } = useQuery({
        queryKey: queryKeys.sites,
        queryFn: welfareApi.getSites,
    });
    const targetReviewed = window.localStorage.getItem('welfare-f5-target-reviewed') === 'true';
    const googleReady = Boolean(currentVerification(config?.revision, homeGoogleCheck)?.ok);
    const onboardSteps = config ? [
        { done: googleReady, label: 'AI 분석을 위한 Gemini 연결', to: '/settings?tab=guide&step=google', action: '연결 안내', optional: true },
        { done: targetReviewed, label: '수집할 지역·카테고리·나이 확인', to: '/settings?tab=guide&step=target', action: '대상 확인' },
        { done: (sites?.length || 0) > 0, label: '기관 홈페이지 1개 이상 등록', to: '/sites?from=guide', action: '사이트 등록' },
    ] : [];
    const requiredOnboardSteps = onboardSteps.filter(step => !step.optional);
    const onboardDone = requiredOnboardSteps.filter(step => step.done).length;
    const showOnboarding = Boolean(config);
    const closeWelcome = () => {
        window.localStorage.setItem('welfare-f5-welcome-seen', 'true');
        setWelcomeDismissed(true);
    };
    const collapseSetupCard = () => {
        window.localStorage.setItem(SETUP_CARD_COLLAPSED_KEY, 'true');
        setSetupCardCollapsed(true);
    };
    // 홈과 새 소식 화면은 같은 후보 캐시를 사용한다.
    const policyQuery = useQuery({
        queryKey: ['dashboardPolicyChanges'],
        queryFn: () => welfareApi.getPolicyChanges(),
        staleTime: 60000,
    });
    const newsQuery = useQuery({
        queryKey: queryKeys.notionCandidates,
        queryFn: welfareApi.getNotionCandidates,
        staleTime: 60000,
    });
    const policyChangeCount = policyQuery.data?.items?.length;
    const notionCandidateCount = newsQuery.data?.count;

    // AI 분석 백로그 (2026-08-06): "수집 완료"와 "AI 구조화 완료"가 다르다는 걸 화면에서
    // 구분하기 위한 지표. 하루 상한 초과 등으로 아직 AI 분석이 안 된 대기 건수.
    const { data: aiBacklogData } = useQuery({
        queryKey: queryKeys.aiBacklogStatus,
        queryFn: welfareApi.getAiBacklogStatus,
        staleTime: 60000,
    });
    const aiBacklogTotal = aiBacklogData?.backlog_total || 0;
    const aiPermanentlyExcluded = aiBacklogData?.permanently_excluded || 0;

    if (isLoading) {
        return (
            <div className="space-y-6 pt-2 lg:pt-6">
                <Skeleton className="h-8 w-48 rounded-xl" />
                <div className="grid grid-cols-2 gap-4"><Skeleton className="h-56 rounded-3xl" /><Skeleton className="h-56 rounded-3xl" /></div>
            </div>
        );
    }

    return (
        <div className="space-y-6 pt-2 lg:pt-6">
            {showOnboarding && !welcomeDismissed && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/35 p-6" role="dialog" aria-modal="true" aria-labelledby="welcome-title">
                    <div className="w-full max-w-lg rounded-[28px] border border-blue-100 bg-white p-7 shadow-2xl dark:border-blue-900/40 dark:bg-gray-900">
                        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50 text-blue-600"><Sparkles className="h-7 w-7" /></div>
                        <p className="mt-5 text-sm font-bold text-blue-600">복지F5에 오신 것을 환영합니다</p>
                        <h2 id="welcome-title" className="mt-1 text-2xl font-bold text-gray-900 dark:text-white">처음 설정을 시작할까요?</h2>
                        <p className="mt-3 text-base leading-7 text-gray-600 dark:text-gray-300">Gemini와 Notion 연결부터 차례로 안내합니다. Notion은 건너뛸 수 있습니다.</p>
                        <div className="mt-5 grid gap-3">
                            <Link to="/settings?tab=guide&step=setup" onClick={closeWelcome} className="rounded-xl bg-blue-600 p-4 text-center font-semibold text-white hover:bg-blue-700">설정 시작하기</Link>
                        </div>
                        <p className="mt-4 text-sm text-gray-500">언제든 설정 도우미에서 이어서 진행할 수 있습니다.</p>
                        <div className="mt-6 flex justify-end gap-2">
                            <button type="button" onClick={closeWelcome} className="rounded-xl px-4 py-2.5 text-sm font-semibold text-gray-500 hover:bg-gray-100">먼저 둘러보기</button>
                        </div>
                    </div>
                </div>
            )}
            <div className="flex items-center justify-between gap-4">
                <h1 aria-label="복지 F5 홈" className="flex items-center gap-2 text-xl font-bold tracking-tight text-gray-800 dark:text-gray-100"><RefreshCw aria-hidden="true" className="h-6 w-6 text-blue-500" strokeWidth={2.2}/><span>복지 <span className="text-blue-500">F5</span></span></h1>
                <Link to="/settings?tab=guide" className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-2 py-2 text-sm text-gray-500 hover:text-blue-600 focus-visible:ring-2 focus-visible:ring-blue-500">
                    <BookOpen className="h-4 w-4" /> 설정 도우미
                </Link>
            </div>

            {/* 첫 실행 온보딩 체크리스트 — 클린 설치 직후(API 키/사이트 미설정) 무엇부터 해야
                할지 바로 보여준다. 심사위원 등 처음 켜는 사람이 텅 빈 대시보드+에러성 문구만
                보는 상황을 막기 위함(2026-07-20). 설정이 갖춰지면 자동으로 사라진다. */}
            {showOnboarding && !setupCardCollapsed && (
                <Card className="border-blue-200 dark:border-blue-900/50 bg-blue-50/60 dark:bg-blue-950/20 shadow-none">
                    <CardContent className="p-4">
                        <div className="mb-3 flex items-start justify-between gap-3">
                            <div className="flex min-w-0 items-center gap-2">
                                <Key className="h-5 w-5 shrink-0 text-blue-600 dark:text-blue-400" />
                                <div>
                                    <p className="font-medium text-gray-800 dark:text-gray-100">새 소식 탐색 준비</p>
                                    <p className="mt-0.5 text-xs text-gray-500">{onboardDone}/{requiredOnboardSteps.length} 완료 · 정책 최신화 준비는 설정 도우미에서 따로 진행합니다.</p>
                                </div>
                            </div>
                            <button type="button" onClick={collapseSetupCard} aria-label="초기 설정 안내 숨기기" aria-expanded="true" className="inline-flex shrink-0 items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-gray-500 hover:bg-white/80 hover:text-gray-700 dark:hover:bg-gray-900/50 dark:hover:text-gray-200">
                                숨기기 <X className="h-3.5 w-3.5" />
                            </button>
                        </div>
                        <div className="space-y-2">
                            {onboardSteps.map((step, i) => (
                                <div key={i} className="flex items-center justify-between gap-3 bg-white/70 dark:bg-gray-900/30 rounded-lg px-3 py-2">
                                    <div className="flex items-center gap-2">
                                        {step.done ? (
                                            <CheckCircle2 className="w-4 h-4 text-emerald-500 flex-shrink-0" />
                                        ) : (
                                            <div className="w-4 h-4 rounded-full border-2 border-gray-300 dark:border-gray-600 flex-shrink-0" />
                                        )}
                                        <span className={`text-sm ${step.done ? 'text-gray-400 line-through' : 'text-gray-700 dark:text-gray-200 font-medium'}`}>
                                            {i + 1}. {step.label}{step.optional ? ' (선택)' : ''}
                                        </span>
                                    </div>
                                    {!step.done && (
                                        <Link to={step.to} className="text-xs text-blue-600 hover:underline flex items-center gap-0.5 whitespace-nowrap">
                                            {step.action || '이동'} <ArrowRight className="w-3 h-3" />
                                        </Link>
                                    )}
                                </div>
                            ))}
                        </div>
                        <p className="text-xs text-gray-400 mt-2">
                            원문 수집은 AI 연결 없이 시작할 수 있습니다. 수집한 자료를 AI로 분석하려면 <a href="https://aistudio.google.com" target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Google AI Studio</a>에서 API 키를 준비해주세요. Notion 정책 최신화 준비는 설정 도우미에서 따로 진행합니다.
                        </p>
                    </CardContent>
                </Card>
            )}

            <HomeWorkspace policyCount={policyChangeCount} newsCount={notionCandidateCount}
                policyStatus={queryCountStatus(policyQuery, policyChangeCount, '검토 대기')}
                newsStatus={queryCountStatus(newsQuery, notionCandidateCount, '등록 후보')}
                isScraping={isScraping} progress={progress} />
            {(policyQuery.isError || newsQuery.isError) && <div role="status" className="flex flex-wrap items-center gap-2 text-sm text-amber-800">
                <span>일부 업무 건수를 확인하지 못했습니다. 각 업무 화면은 계속 열 수 있습니다.</span>
                <button type="button" disabled={policyQuery.isFetching || newsQuery.isFetching} onClick={() => {
                    if (policyQuery.isError) policyQuery.refetch();
                    if (newsQuery.isError) newsQuery.refetch();
                }} className="rounded-lg px-3 py-2 font-medium underline disabled:opacity-50">다시 확인</button>
            </div>}

            {(aiBacklogTotal > 0 || aiPermanentlyExcluded > 0) && (
                <Link to="/notion" className="block rounded-xl border border-amber-100 bg-amber-50/60 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300">
                    AI 분석 대기 {aiBacklogTotal}건{aiPermanentlyExcluded > 0 ? ` · 자동 분석 제외 ${aiPermanentlyExcluded}건` : ''} · 새 소식에서 확인
                </Link>
            )}

            {expiringData?.items?.length > 0 && <details className="rounded-xl border border-gray-200/60 bg-white/40 dark:border-gray-700">
                <summary className="flex cursor-pointer items-center justify-between px-5 py-4 text-sm text-gray-600 dark:text-gray-300"><span>마감 임박 알림</span><span className="text-xs">7일 이내 {expiringData.total}건</span></summary>
                <ul className="max-h-60 overflow-auto border-t border-gray-100 px-5 dark:border-gray-700">
                    {expiringData.items.map(item=><li key={item.id} className="flex items-center justify-between gap-3 border-b border-gray-100 py-3 last:border-0 dark:border-gray-700">
                        <span className="min-w-0 text-sm text-gray-700 dark:text-gray-200">{isRealLink(item.link)?<a href={item.link} target="_blank" rel="noopener noreferrer" className="hover:underline">{item.title}</a>:item.title}</span>
                        <span className="shrink-0 text-xs text-gray-500">마감 {item.deadline}</span>
                    </li>)}
                </ul>
            </details>}

        </div>
    );
};

export default Home;


import { motion } from 'framer-motion';

import React, { useState, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { welfareApi } from '../api/welfare';
import { Card, CardContent } from '../components/ui/Card';
import { Textarea } from '../components/ui/Textarea';
import { Button } from '../components/ui/Button';
import { Skeleton } from '../components/ui/Skeleton';
import { Tooltip } from '../components/ui/Tooltip';
import { Globe, Plus, RefreshCw, Trash2, FlaskConical, XCircle, AlertTriangle, Zap, Search, Download, FolderOpen, ArrowLeft, CheckCircle2 } from 'lucide-react';
import EmptyState from '../components/EmptyState';
import SiteHealthDashboard from '../components/SiteHealthBadge';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import { queryKeys } from '../api/queryKeys';
import { useAiStatus } from '../hooks/useAiStatus';
import { useSiteTest } from '../hooks/useSiteTest';
import QueryStatusNotice from '../components/QueryStatusNotice';
import { getHomepageRegistrationWarning, isLikelyHomepageUrl } from '../utils/siteRegistration';

const Sites = () => {
    const [newUrl, setNewUrl] = useState('');
    const [failedRegistrationUrls, setFailedRegistrationUrls] = useState([]);
    const inputRevision = useRef(0);
    const registrationPending = useRef(false);
    const [searchParams] = useSearchParams();
    const queryClient = useQueryClient();
    const { data: aiStatus } = useAiStatus();
    const fromGuide = searchParams.get('from') === 'guide';

    // 이전 is_running 상태 추적 (true → false 전환 감지용)
    const prevIsRunning = useRef(false);

    useEffect(() => {
        if (!fromGuide) return;
        // 가이드에서 긴 화면을 내려본 뒤 이동해도 3단계 안내 배너부터 보이게 한다.
        document.querySelector('main')?.scrollTo({ top: 0, behavior: 'auto' });
    }, [fromGuide]);

    const sitesQuery = useQuery({
        queryKey: queryKeys.sites,
        queryFn: welfareApi.getSites,
    });
    const { data: sites, isLoading, isError: sitesError } = sitesQuery;
    const siteTest = useSiteTest(sites);

    const { data: batchStatus } = useQuery({
        queryKey: queryKeys.batchStatus,
        queryFn: welfareApi.getBatchStatus,
        // 분석 실행 중일 때만 빠른 폴링(1.5초), 대기 중엔 요청을 줄인다.
        refetchInterval: (query) => query.state.data?.is_running ? 1500 : 15000,
        refetchIntervalInBackground: false,
    });

    // batchStatus.is_running이 true → false로 바뀌는 순간을 감지
    useEffect(() => {
        if (batchStatus === undefined) return;

        const wasRunning = prevIsRunning.current;
        const isNowRunning = batchStatus.is_running;

        if (wasRunning && !isNowRunning) {
            // 배치 분석이 방금 완료됨 → 사이트 목록 즉시 갱신
            queryClient.invalidateQueries({ queryKey: queryKeys.sites });
            queryClient.invalidateQueries({ queryKey: ['defaultNewsCondition'] });

            const { success_count = 0, fail_count = 0, failed_urls = [], cancelled = false, message = '' } = batchStatus;

            setFailedRegistrationUrls(failed_urls);
            if (cancelled || fail_count > 0 || batchStatus.review_count > 0) {
                toast.warning(message || '등록 결과를 확인해주세요.', { duration: 6000 });
            } else {
                toast.success(message || `${success_count}개 주소가 등록되었습니다.`, { duration: 5000 });
            }
        }

        prevIsRunning.current = isNowRunning;
    }, [batchStatus?.is_running]);

    const addSiteMutation = useMutation({
        mutationFn: ({urls}) => welfareApi.addSitesBatch(urls),
        onSuccess: (_data, submitted) => {
            setFailedRegistrationUrls([]);
            prevIsRunning.current = true;
            toast.info('주소 등록을 시작했습니다. 수집 규칙을 확인합니다.');
            if (inputRevision.current === submitted.revision) setNewUrl('');
            // batchStatus 폴링을 즉시 트리거하여 진행 바가 빠르게 나타나도록 함
            queryClient.invalidateQueries({ queryKey: queryKeys.batchStatus });
        },
        onError: (error) => {
            toast.error('사이트 추가 실패: ' + getErrorMessage(error));
        },
        onSettled: () => { registrationPending.current = false; },
    });

    const deleteSiteMutation = useMutation({
        mutationFn: (siteId) => welfareApi.deleteSite(siteId),
        onSuccess: () => {
            toast.success('사이트가 삭제되었습니다.');
            queryClient.invalidateQueries({ queryKey: queryKeys.sites });
            queryClient.invalidateQueries({ queryKey: ['defaultNewsCondition'] });
        },
        onError: (error) => {
            toast.error('삭제 실패: ' + getErrorMessage(error));
        }
    });

    const importSiteMutation = useMutation({
        mutationFn: (file) => welfareApi.importSites(file),
        onSuccess: (data) => {
            if (data.status === 'started') {
                setFailedRegistrationUrls([]);
                prevIsRunning.current = true;
                toast.info(`${data.count}개 주소의 등록을 시작했습니다. 수집 규칙을 확인합니다.${data.skipped ? ` 중복 ${data.skipped}개는 건너뛰었습니다.` : ''}`);
                queryClient.invalidateQueries({ queryKey: queryKeys.batchStatus });
            } else {
                toast.info('이미 등록된 주소입니다. 새로 등록할 주소가 없습니다.');
            }
            queryClient.invalidateQueries({ queryKey: queryKeys.sites });
        },
        onError: (error) => {
            toast.error('일괄 등록 실패: ' + getErrorMessage(error));
        }
    });

    const cancelBatchMutation = useMutation({
        mutationFn: () => welfareApi.cancelBatchAnalysis(),
        onSuccess: () => {
            toast.info('취소 신호를 보냈습니다. 진행 중인 URL 분석이 끝나면 중단됩니다.');
        },
        onError: (error) => {
            toast.error('취소 실패: ' + getErrorMessage(error));
        }
    });

    const handleImportClick = () => {
        document.getElementById('file-upload').click();
    };

    const handleFileChange = (e) => {
        const file = e.target.files[0];
        if (!file) return;
        e.target.value = '';

        if (!/\.(csv|xlsx|xls)$/i.test(file.name)) {
            toast.error('CSV 또는 Excel 파일만 업로드 가능합니다.');
            return;
        }

        if (window.confirm(`${file.name}의 게시판 목록 주소를 등록하고 수집 규칙을 확인할까요? 사이트 이름은 비워도 됩니다. 대표 홈이 아닌 공지사항·모집 안내 목록 주소인지 확인해주세요. 게시글을 확인하지 못한 주소는 ‘수집 확인 필요’로 저장됩니다. 규칙 확인에 AI가 사용될 수 있습니다.`)) {
            importSiteMutation.mutate(file);
        }
        // Reset input
        e.target.value = '';
    };

    const handleDownloadTemplate = async () => {
        try {
            await welfareApi.downloadSiteTemplate('excel');
            toast.success('주소 입력 양식 다운로드가 시작되었습니다.');
        } catch (error) {
            toast.error('주소 입력 양식 다운로드 실패');
        }
    };

    // URL 유효성 검사 헬퍼: http(s):// 형식 확인 + Set으로 중복 제거
    const validateUrls = (rawUrls) => {
        const seen = new Set();
        const valid = [];
        const invalid = [];
        const duplicates = [];
        rawUrls.forEach(url => {
            try {
                const u = new URL(url);
                if (u.protocol === 'http:' || u.protocol === 'https:') {
                    if (seen.has(url)) {
                        duplicates.push(url);
                    } else {
                        seen.add(url);
                        valid.push(url);
                    }
                } else {
                    invalid.push(url);
                }
            } catch {
                invalid.push(url);
            }
        });
        return { valid, invalid, duplicates };
    };

    const handleSubmit = (e) => {
        e.preventDefault();
        if (registrationPending.current) return;
        if (!Array.isArray(sites) || sitesError) {
            toast.warning('사이트 목록을 확인한 뒤 다시 등록해주세요.');
            return;
        }
        const rawUrls = newUrl.split('\n').map(url => url.trim()).filter(url => url.length > 0);
        if (rawUrls.length === 0) return;

        const { valid, invalid, duplicates } = validateUrls(rawUrls);
        if (invalid.length > 0) {
            toast.error(`올바르지 않은 URL ${invalid.length}개:\n${invalid.slice(0, 3).join('\n')}${invalid.length > 3 ? '\n...' : ''}`, { duration: 6000 });
            if (valid.length === 0) return;
        }
        if (duplicates.length > 0) {
            toast.info(`중복 URL ${duplicates.length}건은 제외됩니다.`);
        }
        const existingUrls = new Set(sites?.map(s => s.url) ?? []);
        const alreadyRegistered = valid.filter(u => existingUrls.has(u));
        if (alreadyRegistered.length > 0) {
            toast.warning(`이미 등록된 URL ${alreadyRegistered.length}건은 제외됩니다.`);
        }
        const toAdd = valid.filter(u => !existingUrls.has(u));
        if (toAdd.length === 0) return;
        const homepageWarning = getHomepageRegistrationWarning(toAdd);
        if (homepageWarning && !window.confirm(homepageWarning)) return;
        registrationPending.current = true;
        addSiteMutation.mutate({urls: toAdd, revision: inputRevision.current});
    };



    const container = {
        hidden: { opacity: 0 },
        show: {
            opacity: 1,
            transition: {
                staggerChildren: 0.1
            }
        }
    };

    const itemVariant = {
        hidden: { opacity: 0, y: 20 },
        show: { opacity: 1, y: 0 }
    };

    return (
        <div className="space-y-6">
            {fromGuide && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-blue-200 bg-blue-50 px-4 py-3.5 dark:border-blue-900/50 dark:bg-blue-950/20" role="status">
                    <div className="flex min-w-0 items-start gap-3">
                        <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white text-blue-600 dark:bg-gray-900/60">
                            {(sites?.length || 0) > 0 ? <CheckCircle2 className="h-5 w-5" /> : <Globe className="h-5 w-5" />}
                        </div>
                        <div>
                            <p className="text-sm font-bold text-blue-900 dark:text-blue-100">초기 설정 3단계 · 사이트 등록</p>
                            <p className="mt-0.5 text-sm text-blue-700 dark:text-blue-300">
                                {sitesError ? '사이트 목록을 불러오지 못했습니다. 다시 확인한 뒤 설정을 이어가세요.' : isLoading ? '등록된 사이트를 확인하고 있습니다.' : (sites?.length || 0) > 0
                                    ? `수집 주소 ${sites.length}개가 등록되었습니다. 설정 결과에서 준비가 끝났는지 확인하세요.`
                                    : '기관의 공지사항·새 소식 게시판 주소를 1개 이상 등록한 뒤 설정 도우미로 돌아가세요.'}
                            </p>
                        </div>
                    </div>
                    <Link to={!sitesError && (sites?.length || 0) > 0 ? '/settings?tab=guide&step=complete' : '/settings?tab=guide&step=sites'} className="ml-auto inline-flex min-h-10 shrink-0 items-center gap-2 rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700">
                        <ArrowLeft className="h-4 w-4" /> {!sitesError && (sites?.length || 0) > 0 ? '설정 결과 보기' : '가이드로 돌아가기'}
                    </Link>
                </div>
            )}
            <div className="flex flex-wrap gap-3 justify-between items-center">
                <div>
                    <h1 className="ui-page-title">수집 사이트 등록·관리</h1>
                    <p className="mt-1 text-sm text-gray-500">복지 소식이 올라오는 공지사항·모집 안내 게시판의 목록 주소를 등록하세요.</p>
                </div>
                <Button variant="outline" onClick={() => queryClient.invalidateQueries({ queryKey: queryKeys.sites })}>
                    <RefreshCw className="mr-2 h-4 w-4" /> 새로고침
                </Button>
            </div>

            <QueryStatusNotice query={sitesQuery} label="사이트 목록" />
            <Card>
                <CardContent className="p-6">
                    <h2 className="mb-4 ui-section-title">새 사이트 등록</h2>
                    <form onSubmit={handleSubmit} className="space-y-2">
                        <label htmlFor="site-registration-urls" className="block text-sm font-semibold text-gray-700 dark:text-gray-200">공지사항·새 소식 게시판 주소</label>
                        <div className="grid grid-cols-1 items-stretch gap-4 sm:grid-cols-[minmax(0,1fr)_10rem]">
                        <Textarea
                            id="site-registration-urls"
                            aria-describedby="site-registration-help"
                            value={newUrl}
                            onChange={(e) => { inputRevision.current += 1; setNewUrl(e.target.value); }}
                            placeholder={"예: https://example.com/notices\n    https://another.com/board"}
                            className="min-h-20 max-h-48 resize-y px-4 py-3 text-sm leading-6"
                            rows={2}
                        />
                            <Button type="submit" disabled={isLoading || sitesError || addSiteMutation.isPending || batchStatus?.is_running || aiStatus?.busy}
                                className="h-auto min-h-12 w-full self-stretch whitespace-normal px-4 py-2"
                                title={aiStatus?.busy ? `${aiStatus.job_label || '다른 AI 작업'}이 끝난 후 사용할 수 있습니다` : undefined}>
                                {addSiteMutation.isPending ? '등록 중...' : '주소 등록'}
                            </Button>
                        </div>
                        <p id="site-registration-help" className="text-sm text-gray-600 dark:text-gray-400">공지사항·모집 안내 게시판의 목록 주소를 입력하세요. 여러 주소는 한 줄에 하나씩 입력하세요.</p>
                    </form>

                    {!batchStatus?.is_running && failedRegistrationUrls.length > 0 && <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 dark:border-amber-900 dark:bg-amber-950/20">
                        <p className="text-sm text-amber-800 dark:text-amber-200">등록하지 못한 주소 {failedRegistrationUrls.length}개가 있습니다.</p>
                        <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">주소와 연결 상태를 확인한 뒤 다시 등록해주세요.</p>
                        <details className="mt-2 text-xs"><summary className="cursor-pointer">등록 실패한 주소 확인</summary><ul className="mt-2 space-y-1 break-all">{failedRegistrationUrls.map(url => <li key={url}>{url}</li>)}</ul></details>
                    </div>}
                    {batchStatus?.is_running && (
                        <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/30 rounded-lg animate-in fade-in zoom-in duration-300">
                            <div className="flex justify-between items-center mb-2">
                                <span className="text-sm font-medium text-blue-700 dark:text-blue-300">
                                    {batchStatus.message}
                                </span>
                                <div className="flex items-center gap-3">
                                    <span className="text-sm font-bold text-blue-700 dark:text-blue-300">
                                        {Math.round((batchStatus.progress / Math.max(batchStatus.total, 1)) * 100)}%
                                    </span>
                                    <button
                                        onClick={() => cancelBatchMutation.mutate()}
                                        disabled={cancelBatchMutation.isPending}
                                        className="flex items-center gap-1 text-xs text-red-500 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300 transition-colors disabled:opacity-50"
                                        title="분석 취소"
                                    >
                                        <XCircle size={14} />
                                        취소
                                    </button>
                                </div>
                            </div>
                            <div className="w-full bg-blue-200 dark:bg-blue-800 rounded-full h-2.5 overflow-hidden">
                                <div className="bg-blue-600 h-full rounded-full transition-all duration-500" style={{ width: `${(batchStatus.progress / Math.max(batchStatus.total, 1)) * 100}%` }}></div>
                            </div>
                            <div className="flex gap-4 mt-2 text-xs text-blue-600 dark:text-blue-400 font-medium">
                                <span>성공: {batchStatus.success_count}건</span>
                                <span>실패: {batchStatus.fail_count}건</span>
                            </div>
                            {batchStatus.current_url && (
                                <p className="mt-2 text-xs text-blue-500 dark:text-blue-400 truncate flex items-center gap-1" title={batchStatus.current_url}>
                                    <Search size={12} /> 현재 확인 중: {batchStatus.current_url}
                                </p>
                            )}
                            {batchStatus.fail_count > 0 && batchStatus.failed_urls?.length > 0 && (
                                <details className="mt-2">
                                    <summary className="text-xs text-red-500 dark:text-red-400 cursor-pointer flex items-center gap-1">
                                        <AlertTriangle size={12} /> 등록하지 못한 주소 {batchStatus.fail_count}건 보기
                                    </summary>
                                    <ul className="mt-1 pl-3 text-xs text-red-500 dark:text-red-400 space-y-0.5">
                                        {batchStatus.failed_urls.map((u, i) => (
                                            <li key={i} className="truncate" title={u}>{u}</li>
                                        ))}
                                    </ul>
                                </details>
                            )}
                        </div>
                    )}

                    <div className="flex flex-wrap gap-3 justify-between items-center border-t border-gray-100 dark:border-gray-800 pt-4 mt-4">

                        <div className="flex min-w-0 max-w-full flex-wrap gap-2">
                            <input
                                type="file"
                                id="file-upload"
                                className="hidden"
                                aria-label="등록할 사이트 목록 파일"
                                tabIndex={-1}
                                accept=".csv,.xlsx,.xls"
                                onChange={handleFileChange}
                            />
                            <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                className="h-auto min-h-9 max-w-full whitespace-normal py-2"
                                onClick={handleDownloadTemplate}
                                title="작성 양식 다운로드"
                            >
                                <Download className="mr-2 h-4 w-4 shrink-0" /> 주소 입력 양식 다운로드
                            </Button>
                            <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                className="h-auto min-h-9 max-w-full whitespace-normal py-2"
                                onClick={handleImportClick}
                                disabled={importSiteMutation.isPending || addSiteMutation.isPending || batchStatus?.is_running}
                            >
                                {importSiteMutation.isPending ? <RefreshCw className="animate-spin w-4 h-4 mr-2 shrink-0" /> : <FolderOpen className="mr-2 h-4 w-4 shrink-0" />}
                                파일로 주소 등록
                            </Button>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* 사이트 건강 대시보드 */}
            <SiteHealthDashboard />

            <h2 className="text-xl font-semibold leading-snug text-gray-800 dark:text-gray-200 mt-2 flex items-center gap-2">
                <Globe className="w-5 h-5" />
                등록된 사이트 {Array.isArray(sites) ? `(${sites.length})` : ''}
            </h2>
            <p className="text-sm text-gray-500 -mt-3">
                각 사이트의 주소와 수집 규칙을 보여줍니다. 위 상태판은 최근 수집 결과를 보여주는 별도 영역입니다.
            </p>

            <motion.div
                className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,280px),1fr))] gap-4"
                variants={container}
                initial="hidden"
                animate="show"
            >
                {isLoading ? (
                    [1, 2, 3].map(i => (
                        <Card key={i}>
                            <CardContent className="p-6">
                                <div className="flex items-start gap-4">
                                    <Skeleton className="h-12 w-12 rounded-lg" />
                                    <div className="space-y-2 flex-1">
                                        <Skeleton className="h-5 w-3/4" />
                                        <Skeleton className="h-3 w-1/2" />
                                    </div>
                                </div>
                            </CardContent>
                        </Card>
                    ))
                ) : sitesError && !sites ? null : sites?.length === 0 ? (
                    <div className="col-span-full">
                        <EmptyState
                            icon={Globe}
                            title="등록된 사이트가 없습니다."
                            subtitle="위 입력칸에 공지사항·새 소식 게시판의 목록 주소를 넣어주세요."
                        />
                    </div>
                ) : (
                    sites?.map((site, idx) => (
                        <motion.div key={site.id || idx} variants={itemVariant}>
                            <Card className="relative overflow-hidden group">
                                <CardContent className="p-6">
                                    <div className="flex items-start gap-4">
                                        <div className="shrink-0 p-3 bg-blue-50 text-blue-500 rounded-lg dark:bg-blue-900/30">
                                            <Globe size={24} />
                                        </div>
                                        <div className="flex-1 min-w-0">
                                            <h3 className="break-words text-base font-semibold leading-snug">{site.name}</h3>
                                            <p className="text-xs text-gray-400 break-all">{site.url}</p>
                                            {site.registration_status !== 'basic_verified' && isLikelyHomepageUrl(site.url) && (
                                                <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">대표 홈페이지로 보이는 주소입니다. 공지사항·모집 안내의 게시판 목록 주소인지 확인해주세요.</p>
                                            )}
                                            <div className="mt-2 flex gap-2 flex-wrap">
                                                <span className="text-xs bg-gray-100 dark:bg-gray-700 px-2 py-1 rounded">
                                                    {site.registration_status === 'needs_check' ? '수집 확인 필요' : site.registration_status === 'basic_verified' ? '기본 수집 확인' : '등록됨'}
                                                </span>
                                                {(!site.selectors || Object.keys(site.selectors).length === 0) ? (
                                                    <Tooltip content="기본 링크 탐색으로 수집을 시도합니다. 수집 확인 필요 상태이면 게시글 본문을 아직 확인하지 못한 상태입니다.">
                                                        <span className="text-xs bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300 px-2 py-1 rounded cursor-help flex items-center gap-1">
                                                            <Zap size={11} /> 기본 링크 탐색
                                                        </span>
                                                    </Tooltip>
                                                ) : (
                                                    <span className="text-xs bg-gray-100 dark:bg-gray-700 px-2 py-1 rounded">
                                                        수집 규칙 {Object.keys(site.selectors).length}개
                                                    </span>
                                                )}
                                                {site.max_pages != null && (
                                                    <span className="text-xs bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 px-2 py-1 rounded">
                                                        {site.max_pages}p
                                                    </span>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                    <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
                                        <Tooltip content="수집 규칙 테스트 — 저장되지 않고, 몇 개나 발견되는지만 미리 확인합니다">
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                aria-label={`${site.name} 수집 규칙 테스트`}
                                                className="gap-1.5 text-sm text-blue-600 hover:bg-blue-50 focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300 dark:hover:bg-blue-900/20"
                                                onClick={() => siteTest.start(site.id || idx)}
                                                disabled={sitesError || siteTest.isRunning}
                                            >
                                                <FlaskConical aria-hidden="true" size={16} /> {siteTest.isRunning && siteTest.task?.site_id === site.id ? '점검 중…' : site.registration_status === 'needs_check' ? '수집 다시 확인' : '수집 테스트'}
                                            </Button>
                                        </Tooltip>
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            aria-label={`${site.name} 사이트 삭제`}
                                            className="gap-1.5 text-sm text-gray-500 hover:text-red-600 hover:bg-red-50 focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-400 dark:hover:bg-red-900/20"
                                            onClick={() => {
                                                if (window.confirm(`"${site.name}" 사이트를 삭제하시겠습니까?`)) {
                                                    deleteSiteMutation.mutate(site.id || idx);
                                                }
                                            }}
                                            disabled={sitesError || deleteSiteMutation.isPending}
                                            title="삭제"
                                        >
                                            <Trash2 aria-hidden="true" size={16} /> 삭제
                                        </Button>
                                    </div>
                                </CardContent>
                            </Card>
                        </motion.div>
                    ))
                )}
            </motion.div>
        </div>
    );
};

export default Sites;

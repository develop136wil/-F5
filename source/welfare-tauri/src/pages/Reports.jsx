
import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { Button } from '../components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card';
import { Skeleton } from '../components/ui/Skeleton';
import { Download, FileSpreadsheet, FileText, FileCode, Loader2, Filter, X, BarChart3 } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { toast } from 'sonner';
import { downloadBlob, todayStr } from '../utils/download';
import { categoriesFromSummary } from '../utils/categories';
import QueryStatusNotice from '../components/QueryStatusNotice';

const PIE_COLORS = ['#3182F6', '#6366f1', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b'];

const Reports = () => {
    const [csvLoading, setCsvLoading] = useState(false);
    const [excelLoading, setExcelLoading] = useState(false);
    const [notionLoading, setNotionLoading] = useState(false);
    const [markdownLoading, setMarkdownLoading] = useState(false);
    const [filters, setFilters] = useState({
        category: '',
        status: '',
        date_from: '',
        date_to: '',
        site_name: '',
    });

    const sitesQuery = useQuery({
        queryKey: queryKeys.sites,
        queryFn: welfareApi.getSites,
        refetchOnWindowFocus: true,
    });
    const {data: sites} = sitesQuery;

    // (2026-07-21) 수집 통계 차트 — 대시보드에서 이곳으로 이동. 실무자가 매일 볼 "오늘
    // 할 일" 화면이 아니라 "수집이 얼마나/어디서/언제 됐는지" 보는 운영 통계라서,
    // 보고서 성격의 이 화면이 더 맞는 자리다(대시보드는 액션 카드 위주로 유지).
    const statsQuery = useQuery({
        queryKey: queryKeys.summaryStats,
        queryFn: welfareApi.getSummaryStats,
    });
    const trendsQuery = useQuery({
        queryKey: queryKeys.trends(30),
        queryFn: () => welfareApi.getTrends(30),
    });
    const {data: stats, isLoading: statsLoading} = statsQuery;
    const {data: trends, isLoading: trendsLoading} = trendsQuery;

    const categoryData = (() => {
        if (!stats?.by_category) return [];
        const merged = {};
        for (const [name, value] of Object.entries(stats.by_category)) {
            const key = (name && name !== 'null') ? name : '미분류';
            merged[key] = (merged[key] || 0) + value;
        }
        return Object.entries(merged).map(([name, value]) => ({ name, value }));
    })();
    const filterCategories = categoriesFromSummary(stats, filters.category);

    const siteEntries = Object.entries(stats?.by_site || {}).sort((a, b) => b[1] - a[1]);
    const siteData = (() => {
        if (siteEntries.length <= 8) return siteEntries.map(([name, value]) => ({ name, value }));
        const top = siteEntries.slice(0, 7).map(([name, value]) => ({ name, value }));
        const rest = siteEntries.slice(7).reduce((sum, [, v]) => sum + v, 0);
        return [...top, { name: '기타', value: rest }];
    })();

    const trendData = trends ?? [];

    const activeFilters = Object.values(filters).filter(v => v !== '').length;
    const invalidDateRange = Boolean(filters.date_from && filters.date_to && filters.date_from > filters.date_to);

    const cleanFilters = () => {
        const f = {};
        if (filters.category) f.category = filters.category;
        if (filters.status) f.status = filters.status;
        if (filters.date_from) f.date_from = filters.date_from;
        if (filters.date_to) f.date_to = filters.date_to;
        if (filters.site_name) f.site_name = filters.site_name;
        return f;
    };

    const resetFilters = () => setFilters({ category: '', status: '', date_from: '', date_to: '', site_name: '' });

    const validateDateRange = () => {
        if (invalidDateRange) {
            toast.error('시작일은 종료일보다 이전이어야 합니다.');
            return false;
        }
        return true;
    };

    const runReport = async (apiFn, setLoading, successMsg, errorMsg) => {
        if (!validateDateRange()) return;
        setLoading(true);
        try {
            await apiFn();
            toast.success(successMsg);
        } catch (error) {
            console.error(errorMsg, error);
            toast.error(errorMsg);
        } finally {
            setLoading(false);
        }
    };

    const handleExport = async (format) => {
        const setLoading = format === 'csv' ? setCsvLoading : setExcelLoading;
        const ext = format === 'excel' ? 'xlsx' : 'csv';
        const mime = format === 'excel'
            ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            : 'text/csv';
        toast.info(`${format.toUpperCase()} 리포트 생성 중...`);
        await runReport(
            async () => {
                const blob = await welfareApi.exportReport(format, cleanFilters());
                downloadBlob(blob, `welfare_report_${todayStr()}.${ext}`, mime);
            },
            setLoading,
            '다운로드가 시작되었습니다!',
            '내보내기 실패. 다시 시도해주세요.'
        );
    };

    const handleNotionReport = () => runReport(
        () => welfareApi.generateNotionReport(cleanFilters()),
        setNotionLoading,
        'Notion 리포트가 다운로드되었습니다!',
        '리포트 생성 실패. 데이터가 있는지 확인하세요.'
    );

    const handleMarkdownReport = () => runReport(
        () => welfareApi.generateNotionMarkdown(cleanFilters()),
        setMarkdownLoading,
        'Notion용 Markdown 리포트가 다운로드되었습니다!',
        'Markdown 리포트 생성 실패'
    );

    return (
        <div className="space-y-6">
            <h1 className="ui-page-title">리포트·내보내기</h1>

            {/* 필터 패널 */}
            <Card>
                <CardHeader>
                    <CardTitle as="h2" className="flex items-center justify-between text-xl">
                        <span className="flex items-center gap-2">
                            <Filter className="w-4 h-4" />
                            내보내기 필터
                            {activeFilters > 0 && (
                                <span className="text-xs bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 px-2 py-0.5 rounded-full">
                                    {activeFilters}개 적용
                                </span>
                            )}
                        </span>
                        {activeFilters > 0 && (
                            <button onClick={resetFilters} className="text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1">
                                <X className="w-3 h-3" /> 초기화
                            </button>
                        )}
                    </CardTitle>
                </CardHeader>
                <CardContent>
                    {sitesQuery.isError && <div className="mb-3"><QueryStatusNotice query={sitesQuery} label="사이트 필터" /></div>}
                    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
                        {/* 카테고리 */}
                        <div>
                            <label htmlFor="report-category" className="text-xs text-gray-500 mb-1 block">카테고리</label>
                            <select
                                id="report-category"
                                value={filters.category}
                                onChange={e => setFilters(f => ({ ...f, category: e.target.value }))}
                                className="w-full text-sm border rounded px-2 py-1.5 bg-white dark:bg-gray-800 dark:border-gray-700"
                            >
                                <option value="">전체</option>
                                {filterCategories.map(c => <option key={c} value={c}>{c}</option>)}
                            </select>
                        </div>

                        {/* 상태 */}
                        <div>
                            <label htmlFor="report-status" className="text-xs text-gray-500 mb-1 block">상태</label>
                            <select
                                id="report-status"
                                value={filters.status}
                                onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}
                                className="w-full text-sm border rounded px-2 py-1.5 bg-white dark:bg-gray-800 dark:border-gray-700"
                            >
                                <option value="">전체</option>
                                <option value="active">활성 (마감 전)</option>
                                <option value="closed">마감</option>
                            </select>
                        </div>

                        {/* 수집일 시작 */}
                        <div>
                            <label htmlFor="report-date-from" className="text-xs text-gray-500 mb-1 block">수집일 (시작)</label>
                            <input
                                id="report-date-from"
                                type="date"
                                aria-invalid={invalidDateRange || undefined}
                                aria-describedby={invalidDateRange ? 'report-date-error' : undefined}
                                value={filters.date_from}
                                onChange={e => setFilters(f => ({ ...f, date_from: e.target.value }))}
                                className="w-full text-sm border rounded px-2 py-1.5 bg-white dark:bg-gray-800 dark:border-gray-700"
                            />
                        </div>

                        {/* 수집일 종료 */}
                        <div>
                            <label htmlFor="report-date-to" className="text-xs text-gray-500 mb-1 block">수집일 (종료)</label>
                            <input
                                id="report-date-to"
                                type="date"
                                aria-invalid={invalidDateRange || undefined}
                                aria-describedby={invalidDateRange ? 'report-date-error' : undefined}
                                value={filters.date_to}
                                onChange={e => setFilters(f => ({ ...f, date_to: e.target.value }))}
                                className="w-full text-sm border rounded px-2 py-1.5 bg-white dark:bg-gray-800 dark:border-gray-700"
                            />
                        </div>

                        {/* 사이트 */}
                        <div>
                            <label htmlFor="report-site" className="text-xs text-gray-500 mb-1 block">사이트</label>
                            <select
                                id="report-site"
                                disabled={sitesQuery.isError && !sites}
                                value={filters.site_name}
                                onChange={e => setFilters(f => ({ ...f, site_name: e.target.value }))}
                                className="w-full text-sm border rounded px-2 py-1.5 bg-white dark:bg-gray-800 dark:border-gray-700"
                            >
                                <option value="">전체</option>
                                {sites?.map(s => (
                                    <option key={s.id} value={s.name}>{s.name}</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    {invalidDateRange && (
                        <p id="report-date-error" role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
                            시작일은 종료일보다 이전이어야 합니다. 수집일을 다시 선택해주세요.
                        </p>
                    )}

                    {activeFilters > 0 && (
                        <p className="text-xs text-blue-600 dark:text-blue-400 mt-3">
                            필터 조건이 적용된 데이터만 내보내집니다.
                        </p>
                    )}
                </CardContent>
            </Card>

            <section aria-labelledby="report-export-heading" className="space-y-4">
            <h2 id="report-export-heading" className="ui-section-title">파일로 내보내기</h2>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,240px),1fr))] gap-6">
                {/* Raw Data Export */}
                <Card>
                    <CardHeader>
                        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-base">
                            <span className="flex items-center gap-2">
                                <FileSpreadsheet className="w-5 h-5 text-green-600" />
                                데이터 내보내기
                            </span>
                            {activeFilters > 0 && (
                                <span className="flex items-center gap-1 text-[11px] bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300 px-2 py-0.5 rounded-full font-normal whitespace-nowrap">
                                    <Filter className="w-3 h-3" />
                                    {activeFilters}개 필터
                                </span>
                            )}
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <p className="text-sm text-gray-500">수집된 데이터를 오프라인 분석용으로 다운로드합니다.</p>
                        <div className="flex flex-col gap-2">
                            <Button onClick={() => handleExport('csv')} variant="outline" className="w-full" disabled={csvLoading}>
                                {csvLoading ? (
                                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 생성 중...</>
                                ) : (
                                    <><Download className="mr-2 h-4 w-4" /> CSV로 받기</>
                                )}
                            </Button>
                            <Button onClick={() => handleExport('excel')} className="w-full" disabled={excelLoading}>
                                {excelLoading ? (
                                    <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 생성 중...</>
                                ) : (
                                    <><FileSpreadsheet className="mr-2 h-4 w-4" /> Excel로 받기</>
                                )}
                            </Button>
                        </div>
                    </CardContent>
                </Card>

                {/* Notion-style HTML Report */}
                <Card className="border-blue-100 dark:border-blue-900/30">
                    <CardHeader>
                        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-base">
                            <span className="flex items-center gap-2">
                                <FileText className="w-5 h-5 text-blue-600" />
                                보기용 리포트
                            </span>
                            {activeFilters > 0 && (
                                <span className="flex items-center gap-1 text-[11px] bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300 px-2 py-0.5 rounded-full font-normal whitespace-nowrap">
                                    <Filter className="w-3 h-3" />
                                    {activeFilters}개 필터
                                </span>
                            )}
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <p className="text-sm text-gray-500">
                            분류·대상·소득 등 분석 내용을 표로 정리합니다. 저장한 파일은 인터넷 브라우저로 열어볼 수 있습니다.
                        </p>
                        <Button
                            onClick={handleNotionReport}
                            disabled={notionLoading}
                            className="w-full bg-blue-600 hover:bg-blue-700 mt-4"
                        >
                            {notionLoading ? (
                                <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 생성 중...</>
                            ) : (
                                <><FileText className="mr-2 h-4 w-4" /> 리포트 다운로드</>
                            )}
                        </Button>
                    </CardContent>
                </Card>

                {/* Notion Markdown Report */}
                <Card className="border-indigo-100 dark:border-indigo-900/30">
                    <CardHeader>
                        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-base">
                            <span className="flex items-center gap-2">
                                <FileCode className="w-5 h-5 text-indigo-600" />
                                Notion용 Markdown
                            </span>
                            {activeFilters > 0 && (
                                <span className="flex items-center gap-1 text-[11px] bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300 px-2 py-0.5 rounded-full font-normal whitespace-nowrap">
                                    <Filter className="w-3 h-3" />
                                    {activeFilters}개 필터
                                </span>
                            )}
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <p className="text-sm text-gray-500">
                            Notion에 바로 복사/붙여넣기 할 수 있는 구조화된 Markdown 형식의 리포트를 생성합니다.
                        </p>
                        <Button
                            onClick={handleMarkdownReport}
                            disabled={markdownLoading}
                            className="w-full bg-indigo-600 hover:bg-indigo-700 mt-4"
                        >
                            {markdownLoading ? (
                                <><Loader2 className="mr-2 h-4 w-4 animate-spin" /> 생성 중...</>
                            ) : (
                                <><FileCode className="mr-2 h-4 w-4" /> Markdown 다운로드</>
                            )}
                        </Button>
                    </CardContent>
                </Card>
            </div>
            </section>

            {/* 수집 통계 — 대시보드에서 이동된 운영 통계 3종 */}
            <div>
                <h2 className="ui-section-title flex items-center gap-2 mb-4">
                    <BarChart3 className="w-5 h-5 text-gray-500" /> 수집 통계
                </h2>
                {statsQuery.isError && <div className="mb-4"><QueryStatusNotice query={statsQuery} label="수집 통계" /></div>}
                <div className="grid gap-4 md:grid-cols-2">
                    <Card className="shadow-none">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-base">카테고리 분포</CardTitle>
                        </CardHeader>
                        <CardContent>
                            {statsLoading ? (
                                <Skeleton className="h-[260px] w-full" />
                            ) : statsQuery.isError && !stats ? <p className="py-8 text-sm text-gray-500">통계를 확인할 수 없습니다.</p> : categoryData.length === 0 ? (
                                <div className="h-[260px] flex items-center justify-center text-sm text-gray-400">데이터 없음</div>
                            ) : (
                                <div className="h-[260px] w-full overflow-hidden">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <PieChart>
                                            <Pie
                                                data={categoryData}
                                                dataKey="value"
                                                nameKey="name"
                                                cx="50%"
                                                cy="50%"
                                                outerRadius={90}
                                                innerRadius={50}
                                                paddingAngle={2}
                                                label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
                                                labelLine={false}
                                                style={{ fontSize: 11 }}
                                            >
                                                {categoryData.map((_, i) => (
                                                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                                                ))}
                                            </Pie>
                                            <Tooltip
                                                contentStyle={{ borderRadius: '12px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)', padding: '6px 10px', fontSize: '12px' }}
                                                formatter={(value) => [`${value}건`, '']}
                                            />
                                        </PieChart>
                                    </ResponsiveContainer>
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    <Card className="shadow-none">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-base">사이트별 수집량</CardTitle>
                        </CardHeader>
                        <CardContent className="pl-0">
                            {statsLoading ? (
                                <Skeleton className="h-[260px] w-full" />
                            ) : statsQuery.isError && !stats ? <p className="py-8 text-sm text-gray-500">통계를 확인할 수 없습니다.</p> : siteData.length === 0 ? (
                                <div className="h-[260px] flex items-center justify-center text-sm text-gray-400">데이터 없음</div>
                            ) : (
                                <div className="h-[260px] w-full overflow-hidden">
                                    <ResponsiveContainer width="100%" height="100%">
                                        <BarChart data={siteData} layout="vertical" barSize={18} margin={{ left: 10, right: 20 }}>
                                            <XAxis type="number" stroke="#9ca3af" fontSize={10} tickLine={false} axisLine={false} />
                                            <YAxis
                                                dataKey="name"
                                                type="category"
                                                stroke="#9ca3af"
                                                fontSize={10}
                                                tickLine={false}
                                                axisLine={false}
                                                width={100}
                                                tick={{ fontSize: 10 }}
                                            />
                                            <Tooltip
                                                cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                                                contentStyle={{ borderRadius: '12px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)', padding: '6px 10px', fontSize: '12px' }}
                                                formatter={(value) => [`${value}건`, '수집']}
                                            />
                                            <Bar dataKey="value" radius={[0, 6, 6, 0]}>
                                                {siteData.map((_, i) => (
                                                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                                                ))}
                                            </Bar>
                                        </BarChart>
                                    </ResponsiveContainer>
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </div>

                <Card className="shadow-none mt-4">
                    <CardHeader className="pb-2">
                        <CardTitle className="text-base">일별 수집 추이 (최근 30일)</CardTitle>
                    </CardHeader>
                    {trendsQuery.isError && <div className="px-4 pb-3"><QueryStatusNotice query={trendsQuery} label="수집 추이" /></div>}
                    <CardContent className="pl-0">
                        {trendsLoading ? (
                            <Skeleton className="h-[200px] w-full" />
                        ) : trendsQuery.isError && !trends ? <p className="py-8 text-sm text-gray-500">수집 추이를 확인할 수 없습니다.</p> : trendData.length === 0 ? (
                            <div className="h-[200px] flex items-center justify-center text-sm text-gray-400">
                                수집 데이터가 없습니다.
                            </div>
                        ) : (
                            <div className="h-[200px] w-full overflow-hidden">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={trendData} barSize={18}>
                                        <XAxis
                                            dataKey="day"
                                            stroke="#9ca3af"
                                            fontSize={10}
                                            tickLine={false}
                                            axisLine={false}
                                            interval="preserveStartEnd"
                                            tickFormatter={(v) => {
                                                const p = v ? v.split('-') : [];
                                                return p.length === 3 ? `${parseInt(p[1])}/${parseInt(p[2])}` : v;
                                            }}
                                        />
                                        <YAxis stroke="#9ca3af" fontSize={10} tickLine={false} axisLine={false} allowDecimals={false} />
                                        <Tooltip
                                            cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                                            contentStyle={{ borderRadius: '12px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.1)', padding: '6px 10px', fontSize: '12px' }}
                                            formatter={(value) => [value + '건', '수집']}
                                        />
                                        <Bar dataKey="count" fill="#6366f1" radius={[4, 4, 4, 4]} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>
        </div>
    );
};

export default Reports;

import React, { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import QueryStatusNotice from './QueryStatusNotice';
import {
    Activity, CheckCircle, AlertTriangle, XCircle, HelpCircle,
    ChevronDown, ChevronUp, Clock, Loader2
} from 'lucide-react';

const STATUS_CONFIG = {
    healthy:  { icon: CheckCircle,    color: 'text-green-500',  bg: 'bg-green-50 dark:bg-green-900/20',   label: '정상' },
    warning:  { icon: AlertTriangle,  color: 'text-yellow-500', bg: 'bg-yellow-50 dark:bg-yellow-900/20', label: '주의' },
    critical: { icon: XCircle,        color: 'text-red-500',    bg: 'bg-red-50 dark:bg-red-900/20',       label: '위험' },
    no_data:  { icon: HelpCircle,     color: 'text-gray-400',   bg: 'bg-gray-50 dark:bg-gray-800',        label: '데이터 없음' },
    unknown:  { icon: HelpCircle,     color: 'text-gray-400',   bg: 'bg-gray-50 dark:bg-gray-800',        label: '알 수 없음' },
};

const StatusBadge = ({ status }) => {
    const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.unknown;
    const Icon = cfg.icon;
    return (
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${cfg.bg} ${cfg.color}`}>
            <Icon className="w-3 h-3" />
            {cfg.label}
        </span>
    );
};

// 같은 도메인의 여러 게시판을 각각 별도 사이트로 등록해두면 이름이 동일해 목록에서
// 구분이 안 된다(실측 2026-07-15) — URL에서 실제로 서로 다른 부분을 찾아 이름 옆에 병기.
// 여러 전략을 순서대로 시도해 그룹 내에서 실제로 값이 갈리는 첫 전략을 채택한다.
const _urlStrategies = [
    (u) => {
        try {
            const vals = [...new URL(u).searchParams.values()].filter(v => v && !/^\d+$/.test(v));
            return vals[vals.length - 1] || null;
        } catch { return null; }
    },
    (u) => {
        try {
            const parts = new URL(u).pathname.split('/').filter(Boolean);
            return parts[parts.length - 2] || null;
        } catch { return null; }
    },
    (u) => {
        try {
            const parts = new URL(u).pathname.split('/').filter(Boolean);
            const seg = parts[parts.length - 1] || '';
            return seg.replace(/\.(do|php|jsp|aspx?|html?)$/i, '') || null;
        } catch { return null; }
    },
];

const distinguishLabels = (urls) => {
    for (const strat of _urlStrategies) {
        const labels = urls.map(strat);
        if (labels.every(Boolean) && new Set(labels).size === labels.length) return labels;
    }
    return urls.map((_, i) => `${i + 1}번`);
};

const useDisplayNames = (sites) => useMemo(() => {
    const byName = {};
    sites.forEach(s => { (byName[s.site_name] ||= []).push(s); });
    const labelMap = {};
    Object.values(byName).forEach(group => {
        if (group.length < 2) return;
        const labels = distinguishLabels(group.map(s => s.url || ''));
        group.forEach((s, i) => { labelMap[s.site_id] = labels[i]; });
    });
    return labelMap;
}, [sites]);

const SiteHealthRow = ({ site, suffix }) => {
    const [expanded, setExpanded] = useState(false);

    const historyQuery = useQuery({
        queryKey: queryKeys.siteHealth(site.site_id),
        queryFn: () => welfareApi.getSiteHealth(site.site_id, 10),
        enabled: expanded,
    });
    const {data: history, isLoading: histLoading} = historyQuery;

    return (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <button
                onClick={() => setExpanded(!expanded)}
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
            >
                <div className="flex items-center gap-3 min-w-0 flex-1">
                    <StatusBadge status={site.status} />
                    <span className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                        {site.site_name}
                        {suffix && <span className="text-gray-400 font-normal"> · {suffix}</span>}
                    </span>
                </div>
                <div className="flex items-center gap-4 text-xs text-gray-500 flex-shrink-0">
                    {site.success_rate != null && (
                        <span className={site.success_rate >= 80 ? 'text-green-600' : site.success_rate >= 50 ? 'text-yellow-600' : 'text-red-600'}>
                            {site.success_rate}%
                        </span>
                    )}
                    <span>{site.total_runs_30d}회/30일</span>
                    <span>{site.total_items_found}건 발견</span>
                    {expanded
                        ? <ChevronUp className="w-4 h-4 text-gray-400" />
                        : <ChevronDown className="w-4 h-4 text-gray-400" />
                    }
                </div>
            </button>

            {expanded && (
                <div className="px-4 pb-3 border-t border-gray-100 dark:border-gray-700">
                    {/* 요약 통계 */}
                    <div className="grid grid-cols-4 gap-3 mt-3 mb-3">
                        <div className="text-center">
                            <div className="text-lg font-bold text-gray-800 dark:text-gray-200">{site.success_count}</div>
                            <div className="text-[10px] text-gray-400">성공</div>
                        </div>
                        <div className="text-center">
                            <div className="text-lg font-bold text-red-500">{site.error_count}</div>
                            <div className="text-[10px] text-gray-400">실패</div>
                        </div>
                        <div className="text-center">
                            <div className="text-lg font-bold text-gray-800 dark:text-gray-200">{site.avg_duration_sec}s</div>
                            <div className="text-[10px] text-gray-400">평균 소요</div>
                        </div>
                        <div className="text-center">
                            <div className="text-lg font-bold text-blue-500">{site.total_items_saved}</div>
                            <div className="text-[10px] text-gray-400">저장</div>
                        </div>
                    </div>

                    {/* 최근 실행 이력 */}
                    <h4 className="text-xs font-medium text-gray-500 mb-2">최근 실행 이력</h4>
                    {historyQuery.isError && <QueryStatusNotice query={historyQuery} label="사이트 실행 이력" />}
                    {histLoading ? (
                        <div className="flex items-center gap-2 py-2 text-xs text-gray-400">
                            <Loader2 className="w-3 h-3 animate-spin" /> 로딩...
                        </div>
                    ) : historyQuery.isError && !history ? null : history?.length > 0 ? (
                        <div className="space-y-1 max-h-40 overflow-y-auto">
                            {history.map((run) => (
                                <div key={run.id} className="flex items-center gap-2 text-xs py-1">
                                    {run.status === 'success'
                                        ? <CheckCircle className="w-3 h-3 text-green-500 flex-shrink-0" />
                                        : <XCircle className="w-3 h-3 text-red-500 flex-shrink-0" />
                                    }
                                    <span className="text-gray-400 flex-shrink-0">
                                        {new Date(run.created_at).toLocaleString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                    </span>
                                    <span className="text-gray-600 dark:text-gray-300">
                                        {run.items_found}건 발견
                                    </span>
                                    {run.duration_sec != null && (
                                        <span className="text-gray-400 flex items-center gap-0.5">
                                            <Clock className="w-3 h-3" />{run.duration_sec}s
                                        </span>
                                    )}
                                    {run.error_message && (
                                        <span className="text-red-400 truncate" title={run.error_message}>
                                            {run.error_message.slice(0, 60)}
                                        </span>
                                    )}
                                </div>
                            ))}
                        </div>
                    ) : (
                        <p className="text-xs text-gray-400 py-1">실행 이력 없음</p>
                    )}
                </div>
            )}
        </div>
    );
};

const SiteHealthDashboard = () => {
    const healthQuery = useQuery({
        queryKey: queryKeys.sitesHealth,
        queryFn: welfareApi.getSitesHealth,
        refetchInterval: 60000,
    });
    const {data: healthData = [], isLoading} = healthQuery;

    // (버그 2026-07-16) useDisplayNames(내부 useMemo)가 아래 조건부 return들보다 뒤에
    // 있으면, 로딩 중(hook 미호출)→완료(hook 호출) 전환 시 렌더마다 hook 개수가 달라져
    // React가 즉시 크래시한다(Minified React error #310). 모든 조건부 return보다 먼저,
    // Hooks 규칙대로 무조건 호출해야 한다.
    const labelMap = useDisplayNames(healthData);

    if (isLoading) {
        return (
            <div className="flex items-center gap-2 py-4 text-sm text-gray-400">
                <Loader2 className="w-4 h-4 animate-spin" /> 건강 상태 로딩...
            </div>
        );
    }

    if (!healthData.length) return <QueryStatusNotice query={healthQuery} label="사이트 수집 상태" />;

    const criticalCount = healthData.filter(s => s.status === 'critical').length;
    const warningCount = healthData.filter(s => s.status === 'warning').length;
    const healthyCount = healthData.filter(s => s.status === 'healthy').length;
    // '확인 필요' = 정상이 아닌 모든 상태(위험/주의/데이터없음/알수없음). 정상과 시각적으로
    // 분리해두면 "뭐가 뭔지 모르겠다"는 문제(실측 2026-07-15)가 줄어든다.
    const needsAttention = healthData
        .filter(s => s.status !== 'healthy')
        .sort((a, b) => {
            const order = { critical: 0, warning: 1, no_data: 2, unknown: 3 };
            return (order[a.status] ?? 4) - (order[b.status] ?? 4);
        });
    const healthy = healthData.filter(s => s.status === 'healthy');

    return (
        <div className="mb-8">
            <h2 className="text-xl font-semibold leading-snug text-gray-800 dark:text-gray-200 mb-2 flex items-center gap-2">
                <Activity className="w-5 h-5" />
                사이트 수집 상태
            </h2>
            <p className="text-sm text-gray-500 mb-4">
                각 사이트별 최근 30일 수집 실적입니다. 성공률 · 실행 횟수 · 발견 건수 순으로 표시됩니다.
            </p>
            {healthQuery.isError && <div className="mb-4"><QueryStatusNotice query={healthQuery} label="사이트 수집 상태" /></div>}

            {/* 요약 바 */}
            <div className="flex gap-4 mb-4 text-sm">
                <span className="flex items-center gap-1 text-green-600">
                    <CheckCircle className="w-4 h-4" /> 정상 {healthyCount}
                </span>
                {warningCount > 0 && (
                    <span className="flex items-center gap-1 text-yellow-600">
                        <AlertTriangle className="w-4 h-4" /> 주의 {warningCount}
                    </span>
                )}
                {criticalCount > 0 && (
                    <span className="flex items-center gap-1 text-red-600">
                        <XCircle className="w-4 h-4" /> 위험 {criticalCount}
                    </span>
                )}
            </div>

            {needsAttention.length > 0 && (
                <div className="mb-4">
                    <h3 className="text-base font-semibold text-gray-700 dark:text-gray-300 mb-2">
                        확인 필요 ({needsAttention.length})
                    </h3>
                    <div className="space-y-2">
                        {needsAttention.map((site) => (
                            <SiteHealthRow key={site.site_id} site={site} suffix={labelMap[site.site_id]} />
                        ))}
                    </div>
                </div>
            )}

            {healthy.length > 0 && (
                <div>
                    <h3 className="text-base font-semibold text-gray-700 dark:text-gray-300 mb-2">
                        정상 ({healthy.length})
                    </h3>
                    <div className="space-y-2">
                        {healthy.map((site) => (
                            <SiteHealthRow key={site.site_id} site={site} suffix={labelMap[site.site_id]} />
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
};

export { StatusBadge, SiteHealthDashboard };
export default SiteHealthDashboard;

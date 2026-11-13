import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { History, ChevronDown, ChevronUp, ArrowRight, Loader2 } from 'lucide-react';

const FIELD_LABELS = {
    title: '제목',
    category: '카테고리',
    target: '대상',
    deadline: '마감일',
    support_detail: '지원 내용',
    eligibility_detail: '자격 요건',
    contact: '문의처',
    event_date: '행사일',
    event_end_date: '행사 종료일',
    residence: '거주지',
    target_income: '소득 수준',
    age_detail: '연령',
    page_modified_date: '페이지 수정일',
};

const ChangeDiffView = ({ itemId }) => {
    const [expandedId, setExpandedId] = useState(null);

    const { data: snapshots = [], isLoading } = useQuery({
        queryKey: queryKeys.itemSnapshots(itemId),
        queryFn: () => welfareApi.getItemSnapshots(itemId),
        enabled: !!itemId,
    });

    const { data: diffData, isLoading: diffLoading } = useQuery({
        queryKey: queryKeys.itemDiff(itemId, expandedId),
        queryFn: () => welfareApi.getItemDiff(itemId, expandedId),
        enabled: !!expandedId,
    });

    if (isLoading) {
        return (
            <div className="flex items-center gap-2 py-3 text-sm text-gray-400">
                <Loader2 className="w-4 h-4 animate-spin" />
                변경 이력 로딩...
            </div>
        );
    }

    if (!snapshots.length) return null;

    return (
        <div className="p-5 border-t border-gray-100 dark:border-gray-700">
            <h3 className="text-sm font-bold text-gray-700 dark:text-gray-300 mb-3 flex items-center gap-2">
                <History className="w-4 h-4" />
                변경 내역 ({snapshots.length})
            </h3>
            <div className="space-y-2">
                {snapshots.map((snap) => {
                    const isExpanded = expandedId === snap.id;
                    return (
                        <div key={snap.id} className="border border-gray-200 dark:border-gray-600 rounded-lg overflow-hidden">
                            <button
                                onClick={() => setExpandedId(isExpanded ? null : snap.id)}
                                className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
                            >
                                <div className="flex items-center gap-2 text-sm min-w-0">
                                    <span className="text-xs text-gray-400 flex-shrink-0">
                                        {new Date(snap.created_at).toLocaleDateString('ko-KR')}
                                    </span>
                                    <span className="text-gray-600 dark:text-gray-300 truncate">
                                        {snap.change_summary || '변경 감지'}
                                    </span>
                                </div>
                                {isExpanded
                                    ? <ChevronUp className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                    : <ChevronDown className="w-4 h-4 text-gray-400 flex-shrink-0" />
                                }
                            </button>

                            {isExpanded && (
                                <div className="px-3 pb-3 border-t border-gray-100 dark:border-gray-700">
                                    {diffLoading ? (
                                        <div className="flex items-center gap-2 py-3 text-sm text-gray-400">
                                            <Loader2 className="w-4 h-4 animate-spin" />
                                            비교 중...
                                        </div>
                                    ) : diffData?.changes?.length > 0 ? (
                                        <div className="mt-2 space-y-2">
                                            {diffData.changes.map((change, i) => (
                                                <div key={`${change.field}-${i}`} className="text-sm">
                                                    <span className="font-medium text-gray-500 dark:text-gray-400">
                                                        {FIELD_LABELS[change.field] || change.field}
                                                    </span>
                                                    <div className="flex items-start gap-2 mt-1">
                                                        <span className="bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 px-2 py-1 rounded text-xs flex-1 break-words line-through">
                                                            {change.old || '(없음)'}
                                                        </span>
                                                        <ArrowRight className="w-3 h-3 text-gray-400 mt-1 flex-shrink-0" />
                                                        <span className="bg-green-50 dark:bg-green-900/20 text-green-600 dark:text-green-400 px-2 py-1 rounded text-xs flex-1 break-words">
                                                            {change.new || '(없음)'}
                                                        </span>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    ) : (
                                        <p className="text-xs text-gray-400 py-2">세부 변경 내역 없음</p>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
};

export default ChangeDiffView;

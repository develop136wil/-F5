import React, { useState, useEffect, useLayoutEffect } from 'react';
import { useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { Input } from '../components/ui/Input';
import { Button } from '../components/ui/Button';
import { Badge } from '../components/ui/Badge';
import { Card, CardContent } from '../components/ui/Card';
import { Skeleton } from '../components/ui/Skeleton';
import { FolderOpen, ChevronLeft, ChevronRight, Sparkles, Loader2, AlertTriangle, Calendar as CalendarIcon, Type, CheckCircle2 } from 'lucide-react';
import { Search as SearchIcon, ExternalLink } from 'lucide-react';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import ItemDetailModal from '../components/ItemDetailModal';
import { isRealLink } from '../utils/link';
import { summarizeBackfillOutcome, nextBackfillCursor } from '../utils/embeddingBackfill';
import { describeSearchMatch } from '../utils/searchMatch';
import { categoriesFromSummary } from '../utils/categories';
import { resolveSearchExecution } from '../utils/searchExecution';
import { readSearchSession, updateSearchSession } from '../utils/searchSession';
import { searchPreviewText } from '../utils/searchPresentation';

function useSearchSessionField(name) {
    const [value, setValue] = useState(() => readSearchSession()[name]);
    const update = next => setValue(updateSearchSession(name, next));
    return [value, update];
}

const PAGE_SIZE = 100;

// 마감일까지 남은 일수 계산
const getDaysLeft = (deadline) => {
    if (!deadline) return null;
    const today = new Date(); today.setHours(0,0,0,0);
    const d = new Date(deadline); d.setHours(0,0,0,0);
    return Math.ceil((d - today) / 86400000);
};

// 마감 임박 배지 렌더링
const DeadlineBadge = ({ deadline }) => {
    const days = getDaysLeft(deadline);
    if (days === null) return null;
    if (days < 0) return <span className="text-xs font-bold text-gray-400">마감됨</span>;
    if (days <= 3) return <span className="text-xs font-bold text-red-500">D-{days}</span>;
    if (days <= 7) return <span className="text-xs font-bold text-orange-500">D-{days}</span>;
    return null;
};

const SearchPage = () => {
    const [inputQuery, setInputQuery] = useSearchSessionField('inputQuery');
    const [query, setQuery] = useSearchSessionField('query');
    const [category, setCategory] = useSearchSessionField('category');
    const [page, setPage] = useSearchSessionField('page');
    const [selectedItemId, setSelectedItemId] = useState(null);
    const [activeOnly, setActiveOnly] = useSearchSessionField('activeOnly'); // 만료 제외 토글
    // 처음 쓰는 사람도 자연어 문장으로 바로 찾을 수 있게 통합 검색을 기본값으로 둔다.
    // Gemini를 사용할 수 없을 때도 백엔드가 정확한 단어 검색으로 자동 전환하므로
    // 무료 한도 소진이나 일시 오류가 전체 검색 중단으로 이어지지는 않는다.
    const [semanticMode, setSemanticMode] = useSearchSessionField('semanticMode'); // 의미(임베딩)+정확 단어 통합 검색
    const [backfilling, setBackfilling] = useState(false);
    const queryClient = useQueryClient();
    const { data: stats } = useQuery({
        queryKey: queryKeys.summaryStats,
        queryFn: welfareApi.getSummaryStats,
        staleTime: 60000,
    });
    const categories = categoriesFromSummary(stats, category);

    // Debounce: 500ms 입력 없으면 검색 실행 (최소 2자)
    useEffect(() => {
        const timer = setTimeout(() => {
            const trimmed = inputQuery.trim();
            const next = trimmed.length >= 2 ? trimmed : '';
            if (next !== readSearchSession().query) {
                setQuery(next);
                setPage(0);
            }
        }, 500);
        return () => clearTimeout(timer);
    }, [inputQuery]);

    // 통합 검색이 기본값이어도 검색어 없이 분류만 선택했다면 일반 필터 조회를 실행한다.
    // 예전에는 의미 검색의 최소 2자 조건 때문에 분류 버튼만 누르면 아무 일도 일어나지 않았다.
    const { useSemanticSearch, enabled: searchEnabled } = resolveSearchExecution({
        semanticMode,
        query,
        category,
    });

    const searchKey = useSemanticSearch
        ? ['semanticSearch', query, category, activeOnly]
        : queryKeys.search(query, category, page, activeOnly);
    const searchIdentity = JSON.stringify(searchKey);
    const { data: searchResult, isLoading, isError, error, refetch, isFetching, isPlaceholderData } = useQuery({
        queryKey: searchKey,
        queryFn: ({ signal }) => useSemanticSearch
            ? welfareApi.semanticSearch(query, { category: category || undefined, limit: PAGE_SIZE, activeOnly, signal })
            : welfareApi.searchItems(
                query || undefined,
                category || undefined,
                undefined,
                PAGE_SIZE,
                page * PAGE_SIZE,
                activeOnly,
                signal
            ),
        enabled: searchEnabled,
        placeholderData: keepPreviousData,
    });

    // A placeholder belongs to a different query/page. Do not present its rows,
    // links or counts under the new controls. Same-query refreshes retain data.
    const visibleResult = searchEnabled && !isPlaceholderData ? searchResult : undefined;
    const results = visibleResult?.items || visibleResult || [];
    // 통합 검색 응답은 전체 후보 수(total)와 실제 표시 수(returned/items.length)를
    // 구분한다. 예전처럼 items.length만 쓰면 아래의 "총 N건 중 M건" 안내가
    // 절대로 나타나지 않아 검색 범위를 오해하게 된다.
    const totalItems = useSemanticSearch
        ? (visibleResult?.total ?? results.length)
        : (visibleResult?.total || 0);
    const totalPages = useSemanticSearch ? 1 : Math.ceil(totalItems / PAGE_SIZE);

    // 검색어/카테고리 조합으로 필터가 활성화되었는지 여부
    const hasFilter = query.trim() !== '' || category !== '';

    // 의미 검색 인덱스가 없을 때 "설정에서 실행하세요" 안내만 하고 끝내지 않고, 여기서
    // 바로 만들 수 있게 한다(2026-07-20) — 설정 화면의 backfill 루프와 동일한 로직.
    const handleBackfillNow = async () => {
        setBackfilling(true);
        try {
            let totalDone = 0;
            let guard = 0;
            let lastResult = null;
            let cursor = 0;
            let totalHeld = 0;
            while (guard < 1000) {
                guard += 1;
                const r = await welfareApi.backfillEmbeddings(100, cursor);
                totalDone += r.embedded;
                totalHeld += r.held_for_input_size || 0;
                lastResult = { ...r, held_for_input_size: totalHeld };
                const next = nextBackfillCursor(r, cursor);
                if (next === null) break;
                cursor = next;
            }
            // (2026-08-05, GPT Codex 리뷰) Settings.jsx와 동일한 기준으로 판정 — "생성 0건"이
            // 곧 "이미 최신"은 아니다(이번 배치가 전부 실패했을 수 있음).
            if (lastResult) {
                const outcome = summarizeBackfillOutcome(totalDone, lastResult);
                if (outcome.status === 'success') toast.success(outcome.message);
                else if (outcome.status === 'partial' || outcome.status === 'quota') toast.warning(outcome.message);
                else toast.error(outcome.message);
            }
            queryClient.invalidateQueries({ queryKey: ['semanticSearch'] });
        } catch (error) {
            toast.error('의미 검색 데이터 생성 실패: ' + getErrorMessage(error));
        } finally {
            setBackfilling(false);
        }
    };

    const handleSearch = (e) => {
        e.preventDefault();
        const trimmed = inputQuery.trim();
        setQuery(trimmed.length >= 2 ? trimmed : '');
        setPage(0);
    };

    const container = {
        hidden: { opacity: 0 },
        show: {
            opacity: 1,
            transition: {
                staggerChildren: 0.05
            }
        }
    };

    const parentRef = React.useRef(null);
    const rowVirtualizer = useVirtualizer({
        count: results?.length || 0,
        getScrollElement: () => parentRef.current,
        getItemKey: index => results[index]?.id ?? index,
        estimateSize: () => 160,
        overscan: 5,
    });
    useLayoutEffect(() => {
        // New conditions, including cached pages, always start at their first
        // result. Repeat when replacement rows arrive after the placeholder so
        // measurements from the old list cannot restore its previous offset.
        // A modal must never keep the previous query's selected ID.
        setSelectedItemId(null);
        rowVirtualizer.scrollToOffset(0);
    }, [searchIdentity, searchEnabled, isPlaceholderData]);

    return (
        <div className="space-y-6 max-w-4xl mx-auto h-[calc(100vh-100px)] flex flex-col">
            <div className="space-y-2 flex-shrink-0">
                <h1 className="ui-page-title">통합 검색</h1>
                <p className="text-sm text-gray-500">저장된 복지 정보를 검색합니다.</p>
            </div>

            <Card className="flex-shrink-0">
                <CardContent className="p-4">
                    <form onSubmit={handleSearch} className="space-y-2">
                        <label htmlFor="global-search-input" className="block text-sm font-medium text-gray-700 dark:text-gray-200">복지 정보 검색어</label>
                        <div className="flex flex-wrap gap-2">
                        <Input
                            id="global-search-input"
                            autoFocus
                            placeholder={semanticMode
                                ? '예: 영아 양육 도움'
                                : '예: 청년, 주거'}
                            className="min-w-0 flex-[1_1_16rem] text-lg h-12"
                            value={inputQuery}
                            onChange={(e) => setInputQuery(e.target.value)}
                        />
                        <Button type="submit" size="lg" className="h-12 w-32 shrink-0">
                            <SearchIcon className="mr-2 h-5 w-5" /> 검색
                        </Button>
                        </div>
                    </form>
                    {/* 검색 모드 토글 */}
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                        <button
                            type="button"
                            aria-pressed={!semanticMode}
                            onClick={() => { setSemanticMode(false); setPage(0); }}
                            className={`shrink-0 whitespace-nowrap text-xs px-3 py-1.5 rounded-full border transition-colors flex items-center gap-1 ${
                                !semanticMode
                                    ? 'bg-blue-100 border-blue-400 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
                                    : 'bg-gray-100 border-gray-300 text-gray-500 dark:bg-gray-800 dark:border-gray-600'
                            }`}
                        >
                            <Type size={12} /> 정확한 단어만
                        </button>
                        <button
                            type="button"
                            aria-pressed={semanticMode}
                            onClick={() => { setSemanticMode(true); setPage(0); }}
                            className={`shrink-0 whitespace-nowrap text-xs px-3 py-1.5 rounded-full border transition-colors flex items-center gap-1 ${
                                semanticMode
                                    ? 'bg-purple-100 border-purple-400 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400'
                                    : 'bg-gray-100 border-gray-300 text-gray-500 dark:bg-gray-800 dark:border-gray-600'
                            }`}
                        >
                            <Sparkles size={12} /> 통합 검색 (추천)
                        </button>
                        {semanticMode && (
                            <span className="text-sm text-gray-600 dark:text-gray-400">입력한 단어를 우선하고 비슷한 표현까지 함께 찾습니다</span>
                        )}
                    </div>
                    {useSemanticSearch && visibleResult?.notice && (
                        <div role="status" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200">
                            {visibleResult.notice} 정확한 단어 검색 결과는 계속 표시됩니다.
                        </div>
                    )}
                    <div className="mt-4 flex gap-2 flex-wrap items-center">
                        {['전체', ...categories].map(cat => {
                            const selected = category === (cat === '전체' ? '' : cat);
                            return (
                            <button
                                key={cat}
                                type="button"
                                aria-pressed={selected}
                                className={`min-h-9 rounded-full border px-3 py-1 text-sm font-medium transition-colors ${selected
                                    ? 'border-blue-600 bg-blue-600 text-white'
                                    : 'border-gray-200 bg-gray-100 text-gray-600 hover:bg-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                                onClick={() => {
                                    setCategory(cat === '전체' ? '' : cat);
                                    setPage(0);
                                }}
                            >
                                {cat}
                            </button>
                        );})}
                        <button
                            type="button"
                            aria-pressed={activeOnly}
                            onClick={() => { setActiveOnly(v => !v); setPage(0); }}
                            className={`ml-auto flex items-center gap-1 text-xs px-3 py-1 rounded-full border transition-colors ${
                                activeOnly
                                    ? 'bg-green-100 border-green-400 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                                    : 'bg-gray-100 border-gray-300 text-gray-500 dark:bg-gray-800 dark:border-gray-600'
                            }`}
                        >
                            {activeOnly && <CheckCircle2 className="w-3.5 h-3.5" />}
                            마감된 항목 제외
                        </button>
                    </div>
                </CardContent>
            </Card>

            {/* Results count */}
            {!isLoading && totalItems > 0 && (
                <div className="flex flex-wrap items-baseline gap-3 text-sm text-gray-500 min-h-5 flex-shrink-0 pl-2">
                    <h2 className="ui-section-title">검색 결과</h2>
                    {useSemanticSearch && totalItems > results.length ? (
                        <>총 <span className="font-bold text-gray-700 dark:text-gray-300">{totalItems}</span>건 중 관련도가 높은 <span className="font-bold text-gray-700 dark:text-gray-300">{results.length}</span>건</>
                    ) : (
                        <>총 <span className="font-bold text-gray-700 dark:text-gray-300">{totalItems}</span>건의 결과</>
                    )}
                </div>
            )}

            {searchEnabled && isPlaceholderData && (
                <p role="status" className="text-sm text-gray-600 dark:text-gray-400 flex-shrink-0 pl-2">
                    새 결과를 찾는 중입니다. 이전 조건·페이지의 결과는 잠시 숨깁니다.
                </p>
            )}
            {visibleResult && (isFetching || isError) && (
                <div role={isError ? 'alert' : 'status'} className="text-sm text-gray-600 dark:text-gray-400 flex-shrink-0 pl-2">
                    {isError ? '다시 조회하지 못했습니다. 같은 조건으로 이전에 찾은 결과를 표시합니다.' : '같은 조건으로 다시 확인 중입니다. 이전에 찾은 결과를 표시합니다.'}
                    {isError && <Button type="button" variant="outline" size="sm" className="ml-2" disabled={isFetching} onClick={() => refetch()}>
                        검색 다시 시도
                    </Button>}
                </div>
            )}

            <div
                ref={parentRef}
                aria-label="검색 결과 목록"
                aria-busy={searchEnabled && isFetching}
                className="min-h-[20rem] flex-1 overflow-auto scrollbar-thin scrollbar-thumb-gray-200 dark:scrollbar-thumb-gray-700 rounded-lg pr-4"
            >
                {searchEnabled && (isLoading || isPlaceholderData) ? (
                    <div className="space-y-4">
                        {[1, 2, 3, 4, 5].map(i => (
                            <Card key={i}>
                                <CardContent className="p-6">
                                    <Skeleton className="h-4 w-24 mb-2" />
                                    <Skeleton className="h-6 w-3/4 mb-2" />
                                    <Skeleton className="h-4 w-full" />
                                </CardContent>
                            </Card>
                        ))}
                    </div>
                ) : isError && !visibleResult ? (
                    <div role="alert" className="text-center py-12 text-gray-400">
                        {useSemanticSearch
                            ? <Sparkles className="mx-auto h-12 w-12 mb-4 opacity-40 text-purple-400" />
                            : <SearchIcon className="mx-auto h-12 w-12 mb-4 opacity-40" />}
                        <p className="text-lg font-medium text-gray-500">
                            {useSemanticSearch ? '통합 검색을 사용할 수 없습니다.' : '검색 결과를 불러오지 못했습니다.'}
                        </p>
                        <p className="text-sm mt-1">
                            {/* (2026-08-05) 백엔드가 API 키 없음/일일 한도 소진/일시 오류를
                                구분한 문구를 detail로 내려주므로(GPT Codex 3차 리뷰), 503이면
                                무조건 "키를 등록하라"고 고정 표시하던 것을 없애고 실제 사유를
                                그대로 보여준다. */}
                            {getErrorMessage(error, '잠시 후 다시 시도해주세요.')}
                        </p>
                        <Button type="button" variant="outline" className="mt-3" disabled={isFetching} onClick={() => refetch()}>
                            {isFetching ? '다시 검색 중...' : '검색 다시 시도'}
                        </Button>
                    </div>
                ) : semanticMode && query.trim().length < 2 && !category ? (
                    <div className="text-center py-12 text-gray-400">
                        <Sparkles className="mx-auto h-12 w-12 mb-4 opacity-40 text-purple-400" />
                        <p className="text-lg font-medium text-gray-500">검색어를 입력하세요 (최소 2자).</p>
                        <p className="text-sm mt-1">뜻이 비슷한 항목까지 찾아드립니다.</p>
                    </div>
                ) : results?.length === 0 ? (
                    <div className="text-center py-12 text-gray-400">
                        {useSemanticSearch ? (
                            <>
                                <Sparkles className="mx-auto h-12 w-12 mb-4 opacity-40 text-purple-400" />
                                <p className="text-lg font-medium text-gray-500">의미 검색 결과가 없습니다.</p>
                                <p className="text-sm mt-1">기존 데이터에 의미 검색 데이터가 아직 없을 수 있습니다.</p>
                                <Button onClick={handleBackfillNow} disabled={backfilling} className="mt-3" variant="outline">
                                    {backfilling ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
                                    {backfilling ? '생성 중...' : '지금 의미 검색 데이터 만들기'}
                                </Button>
                            </>
                        ) : hasFilter ? (
                            <>
                                <FolderOpen className="mx-auto h-12 w-12 mb-4 opacity-50" />
                                <p className="text-lg font-medium text-gray-500">검색 결과가 없습니다.</p>
                                <p className="text-sm mt-1">다른 키워드나 카테고리를 시도해보세요.</p>
                            </>
                        ) : (
                            <>
                                <SearchIcon className="mx-auto h-12 w-12 mb-4 opacity-40" />
                                <p className="text-lg font-medium text-gray-500">수집된 데이터가 없습니다.</p>
                                <p className="text-sm mt-1">새 소식 화면에서 ‘새 소식 찾기’를 먼저 실행해보세요.</p>
                            </>
                        )}
                    </div>
                ) : (
                    <div
                        style={{
                            height: `${rowVirtualizer.getTotalSize()}px`,
                            width: '100%',
                            position: 'relative',
                        }}
                    >
                        {rowVirtualizer.getVirtualItems().map((virtualItem) => {
                            const item = results[virtualItem.index];
                            const match = describeSearchMatch(item, useSemanticSearch);
                            return (
                                <div
                                    key={virtualItem.key}
                                    data-index={virtualItem.index}
                                    ref={rowVirtualizer.measureElement}
                                    style={{
                                        position: 'absolute',
                                        top: 0,
                                        left: 0,
                                        width: '100%',
                                        transform: `translateY(${virtualItem.start}px)`,
                                    }}
                                    className="pb-4 px-2"
                                >
                                    <div className="h-full">
                                        <Card
                                            className="h-full hover:border-blue-400 dark:hover:border-blue-600 transition-all cursor-pointer hover:shadow-md active:scale-[0.99] group flex flex-col"
                                            role="button"
                                            tabIndex={0}
                                            aria-label={`${item.title} 상세 정보 보기`}
                                            onClick={(e) => { if (e.target.closest('a')) return; setSelectedItemId(item.id); }}
                                            onKeyDown={(e) => {
                                                if (e.target.closest('a')) return;
                                                if (e.key === 'Enter' || e.key === ' ') {
                                                    e.preventDefault();
                                                    setSelectedItemId(item.id);
                                                }
                                            }}
                                        >
                                            <CardContent className="p-5 flex-1 flex flex-col justify-between">
                                                <div className="flex justify-between items-start mb-2">
                                                    <div className="flex-1 min-w-0 pr-4">
                                                        <div className="flex gap-2 mb-2 items-center">
                                                            <Badge variant="secondary" className="bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 border-none">
                                                                {item.category || '미분류'}
                                                            </Badge>
                                                            {item.site_name && <span className="text-xs text-gray-400 flex items-center">{item.site_name}</span>}
                                                            {match && (
                                                                <span className={`text-[11px] font-semibold flex items-center gap-0.5 ${match.tone === 'strong' ? 'text-emerald-600 dark:text-emerald-400' : match.tone === 'keyword' ? 'text-blue-600 dark:text-blue-400' : 'text-purple-600 dark:text-purple-400'}`} title="이 결과가 검색된 이유">
                                                                    <Sparkles size={11} /> {match.label}
                                                                </span>
                                                            )}
                                                            <DeadlineBadge deadline={item.deadline} />
                                                        </div>
                                                        <h3 className="text-base font-semibold leading-snug mb-1 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors truncate">
                                                            {item.title}
                                                        </h3>
                                                        <p className="text-sm text-gray-600 dark:text-gray-400 line-clamp-2">{searchPreviewText(item)}</p>
                                                    </div>
                                                    {isRealLink(item.link) && (
                                                        <a
                                                            href={item.link}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            aria-label={`${item.title} 원문 새 창에서 열기`}
                                                            className="text-gray-400 hover:text-blue-500 p-2"
                                                        >
                                                            <ExternalLink size={18} />
                                                        </a>
                                                    )}
                                                </div>
                                                <div className="mt-auto text-[11px] text-gray-400 flex gap-4 border-t border-gray-50 dark:border-gray-800 pt-3">
                                                    {item.event_date && <span className="flex items-center gap-1"><CalendarIcon size={11} /> 일정: {item.event_date}</span>}
                                                    {item.deadline && <span className="text-red-500 dark:text-red-400 font-medium flex items-center gap-1"><AlertTriangle size={11} /> 마감: {item.deadline}</span>}
                                                    <span className="ml-auto flex items-center">
                                                        수집: {new Date(item.scraped_at).toLocaleDateString('ko-KR')}
                                                    </span>
                                                </div>
                                            </CardContent>
                                        </Card>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
                <div className="flex justify-center items-center gap-4 py-4">
                    <Button
                        variant="outline"
                        size="sm"
                        disabled={page === 0}
                        onClick={() => setPage(p => Math.max(0, p - 1))}
                    >
                        <ChevronLeft className="h-4 w-4 mr-1" /> 이전
                    </Button>
                    <span className="text-sm text-gray-500">
                        {page + 1} / {totalPages}
                    </span>
                    <Button
                        variant="outline"
                        size="sm"
                        disabled={page >= totalPages - 1}
                        onClick={() => setPage(p => p + 1)}
                    >
                        다음 <ChevronRight className="h-4 w-4 ml-1" />
                    </Button>
                </div>
            )}

            {/* Detail Modal */}
            {selectedItemId && (
                <ItemDetailModal
                    itemId={selectedItemId}
                    onClose={() => setSelectedItemId(null)}
                />
            )}
        </div>
    );
};

export default SearchPage;

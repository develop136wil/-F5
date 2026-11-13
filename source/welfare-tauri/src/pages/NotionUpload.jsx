import React, { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import { isRealLink } from '../utils/link';
import { UploadCloud, RefreshCw, ExternalLink, Loader2, EyeOff, Building2, Calendar, FileDown } from 'lucide-react';
import ItemDetailModal from '../components/ItemDetailModal';
import NewsActionDialog from '../components/NewsActionDialog';
import { createNewsActionGate, snapshotNewsSelection, isNewsSelectionCurrent, newsActionConfirmation } from '../utils/newsAction';
import { formatNewsPeriod } from '../utils/newsPeriod';
import { Link, useSearchParams } from 'react-router-dom';
import NewsCollectionPanel from '../components/NewsCollectionPanel';
import NewsTargetCollection from '../components/NewsTargetCollection';
import NewsFeedDetailDialog from '../components/NewsFeedDetailDialog';
import NewsProcessingRecords from '../components/NewsProcessingRecords';
import { useScrape } from '../context/ScrapeContext';
import { queryKeys } from '../api/queryKeys';

const EMPTY_ITEMS = [];

const STATUS_COLOR = {
    '진행 예정': 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300',
    '진행 중': 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300',
    '종료': 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
};

const NotionUpload = () => {
    const [searchParams] = useSearchParams();
    const queryClient = useQueryClient();
    const { isScraping, isStarting } = useScrape();
    const collectingRef = useRef(false);
    collectingRef.current = isScraping || isStarting;
    const [selected, setSelected] = useState(new Set());
    const [busy, setBusy] = useState(false);
    const [action, setAction] = useState(null);
    const [confirmation, setConfirmation] = useState(null);
    const confirmationRef = useRef(null);
    const returnFocusRef = useRef(null);
    const gate = useRef(null);
    if (!gate.current) gate.current = createNewsActionGate(setBusy);
    const uploading = busy && (action === 'upload' || action === 'retry');
    const exporting = busy && action === 'html';
    const [detailId, setDetailId] = useState(null);
    const [detailItem, setDetailItem] = useState(null);
    const mountedRef = useRef(true);
    const openDetail = item => item.item_id ? setDetailId(item.item_id) : setDetailItem(item);
    const [scope, setScope] = useState({id:'default-news', name:'현재 수집 조건'});
    const [offset, setOffset] = useState(0);
    const [recordsRequest, setRecordsRequest] = useState(null);
    const resultsRef = useRef(null);
    const focusResults = () => requestAnimationFrame(() => {
        resultsRef.current?.scrollIntoView({block:'start', behavior:'auto'});
        resultsRef.current?.focus({preventScroll:true});
    });
    const showResults = (profileId = 'default-news', name = '현재 수집 조건') => {
        setScope({id:profileId,name}); setOffset(0); setSelected(new Set());
        queryClient.invalidateQueries({queryKey:queryKeys.newsFeed}); focusResults();
    };
    const showRecords = view => {
        setScope({id:'default-news',name:'현재 수집 조건'});
        setRecordsRequest(previous=>({view,id:(previous?.id||0)+1}));
    };
    useEffect(() => {
        const profileId = searchParams.get('profile');
        if (profileId) {setScope({id:profileId,name:'저장한 다른 조건'});setOffset(0);setSelected(new Set());focusResults();}
    }, [searchParams]);
    const changePage = next => { setOffset(next); setSelected(new Set()); focusResults(); };

    const { data, isLoading, isError, isFetching } = useQuery({
        queryKey: [...queryKeys.newsFeed, scope.id, 'ready', offset],
        queryFn: ({signal}) => welfareApi.getNewsFeed({profileId:scope.id,view:'ready',offset,signal}),
        refetchInterval: busy ? false : (isScraping || isStarting ? 5000 : 30000),
        refetchIntervalInBackground: false,
    });
    const items = data?.items || EMPTY_ITEMS;
    const configured = data?.configured;
    const itemsRef = useRef(items);
    itemsRef.current = items;
    const selectableItems = items.filter(item => item.selectable === true);
    const selectedIds = selectableItems.filter(item => selected.has(item.id)).map(item => item.id);
    const selectedCount = selectedIds.length;
    const actionsDisabled = busy || isScraping || isStarting || isFetching || isError;

    useEffect(() => {
        if (!data) return;
        const valid = new Set(items.filter(item => item.selectable === true).map(item => item.id));
        setSelected(previous => {
            const next = new Set([...previous].filter(id => valid.has(id)));
            return next.size === previous.size ? previous : next;
        });
    }, [data, items]);
    useEffect(() => {
        if (data && !isFetching && offset > 0 && offset >= data.total)
            setOffset(Math.max(0, Math.floor((Math.max(1,data.total)-1)/10)*10));
    }, [data, isFetching, offset]);
    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; confirmationRef.current?.(false); confirmationRef.current = null; };
    }, []);
    useEffect(() => {
        if (!busy && returnFocusRef.current?.isConnected && document.activeElement === document.body) {
            returnFocusRef.current.focus();
        }
    }, [busy]);

    const answerConfirmation = approved => {
        const resolve = confirmationRef.current;
        confirmationRef.current = null;
        setConfirmation(null);
        resolve?.(approved);
    };

    const runAction = (kind, ids, task) => gate.current.run(async () => {
        if (collectingRef.current) { toast.info('새 소식 찾기가 끝난 뒤 저장·등록할 수 있습니다.'); return; }
        returnFocusRef.current = document.activeElement;
        const snapshot = snapshotNewsSelection(itemsRef.current, ids);
        if (!snapshot.ids.length) { toast.error('처리할 새소식을 선택해주세요'); return; }
        const question = newsActionConfirmation(kind, snapshot);
        if (question && !await new Promise(resolve => {
            confirmationRef.current = resolve;
            setConfirmation(question);
        })) return;
        if (!mountedRef.current) return;
        if (collectingRef.current) { toast.info('새 소식 찾기가 시작됐습니다. 완료 후 다시 확인해주세요.'); return; }
        if (!isNewsSelectionCurrent(snapshot, itemsRef.current)) {
            toast.warning('확인하는 동안 목록이 바뀌었습니다. 내용을 확인한 뒤 다시 선택해주세요.');
            return;
        }
        setAction(kind);
        try {
            const rows = itemsRef.current.filter(item => snapshot.ids.includes(item.id));
            const prepared = await welfareApi.prepareNewsFeedAction(scope.id, kind,
                rows.map(({id,fingerprint}) => ({id,fingerprint})));
            if (!Array.isArray(prepared.ids) || !prepared.ids.length) throw Error('처리할 소식을 다시 확인해주세요.');
            if (!mountedRef.current) return;
            await task(prepared.ids);
        } catch (error) {
            toast.error('선택한 소식을 확인하지 못했습니다: ' + getErrorMessage(error));
        } finally { setAction(null); reload(); }
    });

    const reload = () => {
        queryClient.invalidateQueries({queryKey:queryKeys.newsFeed});
        queryClient.invalidateQueries({queryKey:queryKeys.notionCandidates});
    };

    const toggle = (id) => setSelected(prev => {
        const n = new Set(prev);
        n.has(id) ? n.delete(id) : n.add(id);
        return n;
    });
    const allSelected = selectableItems.length > 0 && selectedCount === selectableItems.length;
    const toggleAll = () => setSelected(allSelected ? new Set() : new Set(selectableItems.map(i => i.id)));

    const handleVerify = async () => {
        const t = toast.loading('Notion 연결 확인 중...');
        try {
            const r = await welfareApi.verifyNotion();
            if (r.ok) toast.success(`연결 성공 — DB "${r.title}"`, { id: t });
            else toast.error(`연결 실패: ${r.error}`, { id: t });
        } catch (e) { toast.error('확인 실패: ' + getErrorMessage(e), { id: t }); }
    };

    const handleDismiss = (ids, e) => {
        e?.stopPropagation();
        return runAction('dismiss', ids, async ids => {
            try {
                await welfareApi.dismissNotionCandidates(ids);
                toast.success(`${ids.length}건을 후보에서 제외했습니다`);
                setSelected(new Set());
                reload();
            } catch (err) {
                toast.error('제외 실패: ' + getErrorMessage(err));
            }
        });
    };

    const handleUpload = () => runAction('upload', selectedIds, async ids => {
        const t = toast.loading(`${ids.length}건 Notion 업로드 중… (포스터 이미지를 확인하느라 잠시 걸릴 수 있어요)`);
        try {
            const r = await welfareApi.uploadToNotion(ids);
            const failed = r.total - r.uploaded;
            if (failed === 0) toast.success(`${r.uploaded}건 업로드 완료`, { id: t });
            else toast.warning(`${r.uploaded}건 완료, ${failed}건 실패`, {
                id: t, description: r.results?.filter(x => !x.ok).map(x => x.error).join('\n'), duration: 10000,
            });
            setSelected(new Set());
            reload();
        } catch (e) {
            toast.error('업로드 실패: ' + getErrorMessage(e), { id: t });
        } finally {
            reload();
        }
    });

    const handleHtmlExport = () => runAction('html', selectedIds, async ids => {
        const t = toast.loading(`${ids.length}건의 소식 모음 파일을 만드는 중… 포스터가 없는 페이지는 한 번만 확인합니다.`);
        try {
            const result = await welfareApi.exportNewsGallery(ids);
            const posterText = result.posters > 0 ? ` · 포스터 ${result.posters}건 포함` : '';
            const cacheText = result.cacheHits > 0 ? ` · 이전 확인 ${result.cacheHits}건 재사용` : '';
            toast.success(`소식 모음 파일 다운로드를 요청했습니다${posterText}${cacheText} · AI 사용 0회`, { id: t, duration: 7000 });
        } catch (error) {
            toast.error('소식 모음 파일 저장 실패: ' + getErrorMessage(error), { id: t });
        }
    });

    const retryIds = selectableItems.filter(item => selected.has(item.id) && item.upload_uncertain).map(item => item.id);
    const allowRetry = () => runAction('retry', retryIds, async ids => {
        try {
            await welfareApi.allowNotionUploadRetry(ids);
            toast.info('재시도 잠금을 해제했습니다. 선택 업로드를 누르면 다시 시도합니다.');
        } catch (error) { toast.error(getErrorMessage(error)); }
    });

    return (
        <div className="space-y-6 max-w-5xl mx-auto">
            <div className="flex flex-wrap gap-3 justify-between items-start">
                <div>
                    <h1 className="ui-page-title">
                        새 소식
                    </h1>
                    <p className="text-sm text-gray-500 mt-1">
                        등록한 기관 게시판에서 소식을 찾아 한곳에 모읍니다. 필요한 소식만 선택해 저장하세요.
                    </p>
                </div>
            </div>

            <div id="news-collection" tabIndex={-1} className="focus:outline-none"><NewsCollectionPanel actionBusy={busy} onResults={showResults} onRecords={showRecords} conditionActions={<NewsTargetCollection actionBusy={actionsDisabled} onResults={showResults}/>} /></div>

            <section aria-labelledby="news-results-title" aria-busy={isFetching} className="space-y-4">
            <header className="space-y-1">
                <div className="flex items-center justify-between gap-3">
                <h2 ref={resultsRef} tabIndex={-1} id="news-results-title" className="text-xl font-semibold leading-7 text-gray-900 focus:outline-none dark:text-white">찾은 소식{!isLoading&&!isError&&!isFetching ? ' ' + (data?.total || 0) + '건' : ''}</h2>
                    <button type="button" disabled={busy||isFetching} onClick={reload} aria-label="목록 새로고침" title="목록 새로고침" className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800"><RefreshCw aria-hidden="true" className={`h-4 w-4 ${isFetching ? 'animate-spin motion-reduce:animate-none' : ''}`}/></button>
                </div>
                {items.length > 0 && <p className="mt-1 text-sm leading-6 text-gray-500">소식을 눌러 확인한 뒤, 필요한 항목을 선택해 저장하세요.</p>}
                {scope.id !== 'default-news' && <p className="text-sm text-blue-700">{scope.name} <button type="button" disabled={busy} className="ml-2 underline" onClick={()=>showResults()}>현재 수집 조건으로 돌아가기</button></p>}
                {items.length > 0 && configured === true && <details className="pt-1 text-xs text-gray-500">
                    <summary className="w-fit cursor-pointer py-1">Notion 연결 도움말</summary>
                    <p className="mt-1">선택한 소식만 등록하며, 자동으로 등록하지 않습니다.</p>
                    <button type="button" onClick={handleVerify} disabled={actionsDisabled} className="mt-1 min-h-10 rounded px-1 text-blue-600 underline disabled:opacity-50">Notion 연결 확인</button>
                </details>}
            {items.length > 0 && configured === false && (
                <p className="text-sm text-gray-500">소식 모음 파일 저장은 Notion 없이도 사용할 수 있습니다. <Link to="/settings?tab=guide&step=news" className="text-blue-600 underline underline-offset-2 dark:text-blue-400">Notion 연결하기</Link></p>
            )}
            </header>

            {/* Toolbar */}
            {items.length > 0 && <div role="group" aria-label="소식 선택 및 저장" className="flex flex-wrap gap-3 items-center justify-between bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 px-4 py-3">
                <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300 cursor-pointer">
                    <input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={actionsDisabled} className="w-4 h-4 accent-emerald-600" />
                    현재 페이지 선택 ({selectedCount}/{selectableItems.length})
                </label>
                <div className="flex flex-wrap gap-2">
                    <button onClick={(e) => handleDismiss(selectedIds, e)} disabled={actionsDisabled || selectedCount === 0}
                        title="선택한 항목을 후보에서 영구 제외"
                        className="px-3 py-2 border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 flex items-center gap-1.5 text-sm">
                        <EyeOff className="w-4 h-4" /> 선택 제외
                    </button>
                    {items.some(it => it.upload_uncertain) && <button onClick={allowRetry} disabled={actionsDisabled || !items.some(it => selected.has(it.id) && it.upload_uncertain)}
                        className="px-3 py-2 border rounded-lg text-sm disabled:opacity-50">
                        Notion 확인 후 재시도 허용
                    </button>}
                    <button onClick={handleHtmlExport} disabled={actionsDisabled || selectedCount === 0}
                        title="선택한 기존 자료로 소식 모음 파일을 만듭니다. Gemini API는 사용하지 않습니다."
                        className="px-4 py-2 border border-blue-200 bg-blue-50 text-blue-700 rounded-lg hover:bg-blue-100 disabled:opacity-50 flex items-center gap-2 text-sm dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-300">
                        {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                        소식 모음 파일 저장 ({selectedCount})
                    </button>
                    <button onClick={handleUpload} disabled={actionsDisabled || selectedCount === 0 || !configured}
                        className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-2 text-sm">
                        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <UploadCloud className="w-4 h-4" />}
                        Notion에 등록 ({selectedCount})
                    </button>
                </div>
            </div>}

            {busy && !confirmation && <p role="status" className="text-sm text-blue-700 dark:text-blue-300">요청을 처리하고 있습니다. 완료될 때까지 선택과 제외는 잠시 기다려주세요.</p>}
            {(isScraping || isStarting) && <p className="text-sm text-gray-500">찾는 동안 기존 내용은 미리 볼 수 있습니다. 저장·등록은 찾기가 끝난 뒤 진행해주세요.</p>}

            {items.some(it => it.upload_uncertain) && <p className="text-sm text-amber-700 dark:text-amber-400">
                결과 확인이 필요한 업로드가 있습니다. Notion에 이미 생성됐다면 해당 항목을 선택 제외하세요.
                생성되지 않았다면 해당 항목을 선택하고 ‘Notion 확인 후 재시도 허용’을 눌러주세요.
            </p>}
            {/* List */}
            <div aria-label="찾은 소식 목록" className="space-y-2">
                {isLoading ? (
                    <div role="status" className="p-6 text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 소식 목록을 불러오고 있습니다.</div>
                ) : isError ? (
                    <div role="alert" className="rounded-xl border border-red-100 bg-red-50 p-4 text-sm text-red-700">후보 목록을 불러오지 못했습니다. <button type="button" onClick={reload} className="underline">다시 불러오기</button></div>
                ) : items.length === 0 ? (
                    <div className="p-8 text-center text-gray-400 bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700">
                        {isScraping || isStarting ? '새 소식을 찾고 있습니다. 확인한 소식은 여기에 표시됩니다.' : scope.id !== 'default-news' ? '이 조건으로 표시할 소식이 없습니다.' : '아직 표시할 소식이 없습니다. 위의 수집 범위와 사이트 등록 상태를 확인해주세요.'}
                    </div>
                ) : items.map(it => {
                    const on = selected.has(it.id);
                    return (
                        <article key={it.id} aria-label={`${it.title} 소식`} data-news-item
                            onClick={(e) => { if (e.target.closest('a, button, input, details')) return; openDetail(it); }}
                            className={`grid grid-cols-[16px_minmax(0,1fr)_36px] items-start gap-x-3 bg-white dark:bg-[#1e2128] rounded-xl border p-4 cursor-pointer transition-colors ${on ? 'border-emerald-400 ring-1 ring-emerald-300 dark:ring-emerald-700' : 'border-gray-100 dark:border-gray-700 hover:border-gray-300'}`}>
                                <input type="checkbox" checked={on} onChange={() => toggle(it.id)} onClick={e => e.stopPropagation()} disabled={actionsDisabled || it.selectable !== true}
                                    title="업로드 대상으로 선택"
                                    aria-label={`${it.title} 업로드 대상으로 선택`}
                                    className="w-4 h-4 mt-1 accent-emerald-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500" />
                                <div className="min-w-0 space-y-1">
                                    <div data-news-title className="flex flex-wrap items-start gap-x-2 gap-y-1">
                                        <button type="button" onClick={()=>openDetail(it)} aria-label={`${it.title} 전체 내용 미리보기`}
                                            className="min-w-0 max-w-full break-words [overflow-wrap:anywhere] rounded text-left text-base font-medium leading-6 text-gray-800 dark:text-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500">{it.title}</button>
                                        <span className={`shrink-0 text-xs leading-5 px-2 py-0.5 rounded-full ${STATUS_COLOR[it.status] || ''}`}>{it.status}</span>
                                        {isRealLink(it.link) && (
                                            <a href={it.link} target="_blank" rel="noopener noreferrer" aria-label={`${it.title} 원문 새 창에서 열기`}
                                                className="inline-flex h-6 shrink-0 items-center rounded text-gray-400 hover:text-emerald-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500"><ExternalLink size={13} /></a>
                                        )}
                                    </div>
                                    {it.review_required && <p className="text-xs leading-5 text-amber-800 dark:text-amber-300">확인 필요 · 근거가 부족한 부분을 확인하고 저장하세요.</p>}
                                    {!it.selectable && it.input_note && <p className="text-xs leading-5 text-gray-600 dark:text-gray-300">{it.input_note}</p>}
                                    <div data-news-meta className="flex flex-wrap gap-x-3 gap-y-1 text-xs leading-5 text-gray-500">
                                        <span className="flex min-w-0 items-start gap-1"><Building2 aria-hidden="true" size={12} className="mt-1 shrink-0" /><span className="min-w-0 break-words [overflow-wrap:anywhere]">{it.org || '-'}</span></span>
                                        <span className="flex min-w-0 items-start gap-1"><Calendar aria-hidden="true" size={12} className="mt-1 shrink-0" /><span className="min-w-0 break-words">{formatNewsPeriod(it.start, it.end)}</span></span>
                                    </div>
                                    {it.upload_uncertain && <p className="text-xs leading-5 font-medium text-amber-700 dark:text-amber-400">이전 업로드 결과 확인 필요</p>}
                                    {(it.attachment_review_required || it.input_review_required) && <details className="mt-2 text-xs text-amber-800 dark:text-amber-300">
                                        <summary className="w-fit cursor-pointer rounded py-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500">원문 확인 필요</summary>
                                        <div className="mt-1 space-y-1 break-words leading-5">
                                            {it.attachment_review_required && <p>{it.attachment_note || '일부 첨부파일을 읽지 못했습니다. 등록 전 원문을 확인해주세요.'}</p>}
                                            {it.input_review_required && <p>{it.input_note || 'AI가 전체 내용을 확인하지 못했습니다. 원문을 확인해주세요.'}</p>}
                                        </div>
                                    </details>}
                                </div>
                                <button type="button" onClick={(e) => handleDismiss([it.id], e)} disabled={actionsDisabled || it.selectable !== true} title="후보에서 제외 (다시 안 뜸)" aria-label={`${it.title} 후보에서 제외`}
                                    className="-mt-1.5 inline-flex h-9 w-9 items-center justify-center rounded-lg text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500"><EyeOff size={16} /></button>
                        </article>
                    );
                })}
            </div>
            <NewsFeedPagination offset={offset} total={data?.total} hasMore={data?.has_more} busy={actionsDisabled} onChange={changePage}/>
            <NewsProcessingRecords key={scope.id} profileId={scope.id} counts={isError ? undefined : data?.counts} disabled={busy} onDetail={openDetail} openRequest={scope.id==='default-news'?recordsRequest:null}/>
            </section>

            {detailId && (
                <ItemDetailModal itemId={detailId} onClose={() => setDetailId(null)} tone="news" />
            )}
            {detailItem && <NewsFeedDetailDialog item={{...detailItem,profile_id:detailItem.profile_id || scope.id}} onClose={()=>setDetailItem(null)}/>}
            {confirmation && <NewsActionDialog confirmation={confirmation} onAnswer={answerConfirmation} />}
        </div>
    );
};

export default NotionUpload;

function NewsFeedPagination({offset,total,hasMore,busy,onChange}) {
    if (!hasMore && offset === 0 && (!Number.isInteger(total) || total <= 10)) return null;
    return <nav aria-label="소식 페이지" className="flex flex-wrap items-center gap-3 text-sm text-gray-600">
        <button type="button" disabled={busy || offset===0} onClick={()=>onChange(Math.max(0,offset-10))} className="min-h-10 rounded-lg border border-gray-200 px-3 py-2 disabled:opacity-40">이전 10건</button>
        <span>{Number.isInteger(total) ? (total ? (offset+1) + '–' + Math.min(offset+10,total) + ' / ' + total + '건' : '0건') : '확인 중'}</span>
        <button type="button" disabled={busy || !hasMore} onClick={()=>onChange(offset+10)} className="min-h-10 rounded-lg border border-gray-200 px-3 py-2 disabled:opacity-40">다음 10건</button>
    </nav>;
}

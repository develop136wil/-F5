import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import { X, ExternalLink, Loader2 } from 'lucide-react';
import { welfareApi } from '../api/welfare';
import { checkedDetailResponse, detailEntries, detailOmissions } from '../utils/newsResultDetails';
import { getErrorMessage } from '../utils/error';
import { isRealLink } from '../utils/link';
import ReadableMarkdown from './ReadableMarkdown';
import NewsSupportContent from './NewsSupportContent';

// Saved analysis only: opening a detail never starts AI or materializes a candidate.
export default function NewsFeedDetailDialog({item, onClose}) {
    const dialogRef = useRef(null), closeButtonRef = useRef(null), titleId = useId();
    const canFetch = typeof item.record_id === 'string' && item.record_id.startsWith('job:')
        && item.state === 'complete' && !['invalid_saved_result','unsupported_analysis'].includes(item.review_reason);
    const detail = useQuery({
        queryKey:['newsFeedDetail',item.profile_id,item.record_id,item.fingerprint],
        enabled:canFetch,
        queryFn:async ({signal}) => checkedDetailResponse(
            await welfareApi.getNewsResultDetails(item.profile_id, item.record_id, {signal}), item.record_id),
        retry:false,
    });
    const entries = detailEntries(detail.data?.details);
    const properties = entries.filter(({key}) => key !== 'category' && key !== 'support_detail');
    const support = entries.find(({key}) => key === 'support_detail')?.text || '';
    const category = entries.find(({key}) => key === 'category')?.text;
    const omissions = detailOmissions(detail.data?.detail_omissions);
    const loading = canFetch && detail.isLoading;
    let siteName = item.site_name || item.org || '';
    if (!siteName && isRealLink(item.link)) {
        try { siteName = new URL(item.link).hostname; } catch { /* No invented site. */ }
    }
    useEffect(() => {
        const previous = document.activeElement, dialog = dialogRef.current;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        dialog.showModal();
        closeButtonRef.current?.focus();
        return () => {
            if (dialog.open) dialog.close();
            document.body.style.overflow = previousOverflow;
            if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
        };
    }, []);
    return createPortal(<dialog ref={dialogRef} aria-modal="true" aria-labelledby={titleId}
        onCancel={event=>{event.preventDefault();onClose();}}
        onKeyDown={event=>{
            if (event.key !== 'Tab') return;
            const focusable = [...event.currentTarget.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            )].filter(node => node.tabIndex >= 0 && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden');
            const first = focusable[0], last = focusable[focusable.length - 1];
            if (!first) { event.preventDefault(); return; }
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }}
        onClick={event=>{if(event.target === event.currentTarget) onClose();}}
        className="m-auto max-h-[85dvh] w-[calc(100%-2rem)] max-w-2xl overflow-hidden rounded-xl border-0 bg-white p-0 text-gray-900 shadow-2xl backdrop:bg-black/50 backdrop:backdrop-blur-sm dark:bg-gray-800 dark:text-gray-100">
        <div className="flex max-h-[85dvh] flex-col">
            <header className="flex max-h-[30dvh] shrink-0 items-start justify-between overflow-y-auto border-b border-gray-100 p-5 dark:border-gray-700">
                <div className="min-w-0 flex-1 pr-4">
                    {(category || siteName) && <div className="mb-2 flex flex-wrap items-center gap-2">
                        {category && <span className="rounded bg-teal-100 px-2 py-0.5 text-xs font-medium text-teal-700 dark:bg-teal-900/40 dark:text-teal-300">{category}</span>}
                        {siteName && <span className="break-words text-xs text-gray-400">{siteName}</span>}
                    </div>}
                    <h2 id={titleId} className="break-words text-lg font-bold leading-tight text-gray-800 dark:text-gray-100">{item.title}</h2>
                </div>
                <button ref={closeButtonRef} type="button" autoFocus onClick={onClose} aria-label="상세 정보 닫기"
                    className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg transition-colors hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-500 dark:hover:bg-gray-700">
                    <X className="h-5 w-5 text-gray-400" aria-hidden="true"/>
                </button>
            </header>
            <div data-news-detail-body className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                {(item.review_required || item.input_review_required || item.attachment_review_required) &&
                    <div className="m-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-300">
                        <p className="font-semibold">원문 확인이 필요해요</p>
                        <p className="mt-1 break-words">{item.input_note || item.attachment_note || '일부 근거가 부족합니다. 해당 부분은 원문과 비교해 확인해주세요.'}</p>
                    </div>}
                {loading && <div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-gray-500">
                    <Loader2 className="h-8 w-8 animate-spin text-teal-500" aria-hidden="true"/>분석 내용을 불러오고 있습니다.
                </div>}
                {detail.isError && <p role="alert" className="p-5 text-sm text-red-700">{getErrorMessage(detail.error)} <button type="button" onClick={()=>detail.refetch()} className="underline">다시 불러오기</button></p>}
                {(properties.length > 0 || item.published_date) && <dl className="border-b border-gray-100 dark:border-gray-700">
                    {properties.map(({key,label,text})=><PropertyRow key={key} label={label}><ReadableMarkdown text={text}/></PropertyRow>)}
                    {item.published_date && <PropertyRow label="게시일">{item.published_date}</PropertyRow>}
                </dl>}
                <NewsSupportContent supportDetail={support} presentationKind={detail.data?.presentation_kind} sourceNote={detail.data?.source_note}/>
                {!loading && !detail.isError && !entries.length && !detail.data?.source_note && <p className="p-5 text-sm leading-6 text-gray-600">{item.input_note || '아직 표시할 분석 내용이 없습니다. 게시일·본문 확인이 필요한 자료는 원문에서 확인할 수 있습니다.'}</p>}
                {omissions.map(({key,label})=><p key={key} className="px-5 py-2 text-sm text-amber-800">{label}: 전체 내용을 표시하지 못했습니다. 원문을 확인해주세요.</p>)}
            </div>
            {isRealLink(item.link) && <footer className="flex shrink-0 justify-end border-t border-gray-100 p-4 dark:border-gray-700">
                <a href={item.link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 rounded-lg bg-teal-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-teal-800">
                    <ExternalLink className="h-4 w-4" aria-hidden="true"/>원문 보기
                </a>
            </footer>}
        </div>
    </dialog>,document.body);
}

function PropertyRow({label, children}) {
    return <div className="flex min-h-[36px] border-b border-gray-100 dark:border-gray-700">
        <dt className="w-36 shrink-0 px-3 py-2 text-sm font-medium text-gray-500 dark:text-gray-400">{label}</dt>
        <dd className="min-w-0 flex-1 break-words px-3 py-2 text-sm text-gray-800 [overflow-wrap:anywhere] dark:text-gray-200">{children}</dd>
    </div>;
}

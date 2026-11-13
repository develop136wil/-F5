import React, { useEffect, useId, useRef, useState } from 'react';
import { welfareApi } from '../api/welfare';
import { X, ExternalLink, Loader2, AlertTriangle } from 'lucide-react';
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion';
import ChangeDiffView from './ChangeDiffView';
import { isRealLink } from '../utils/link';
import NewsSupportContent from './NewsSupportContent';
import { storedNewsPresentation } from '../utils/newsResultDetails';

const ItemDetailModal = ({ itemId, onClose, tone = 'default' }) => {
    const titleId = useId();
    const reduceMotion = useReducedMotion();
    const [item, setItem] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);
    const [similarItems, setSimilarItems] = useState([]);
    const [similarLoading, setSimilarLoading] = useState(false);
    const dialogRef = useRef(null);
    const closeButtonRef = useRef(null);
    const onCloseRef = useRef(onClose);

    useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

    useEffect(() => {
        if (!itemId) return;
        const controller = new AbortController();
        setLoading(true);
        setError(false);
        setSimilarItems([]);

        welfareApi.getItemDetail(itemId)
            .then(data => {
                if (controller.signal.aborted) return;
                setItem(data);
                // 상세 정보가 실제로 준비된 뒤 관련 항목을 요청한다. 예전에는 최초 렌더의
                // loading=true 상태에서 effect가 끝난 뒤 다시 실행되지 않아 영원히 비어 있었다.
                setSimilarLoading(true);
                welfareApi.getSimilarItems(itemId, 5)
                    .then(similar => { if (!controller.signal.aborted) setSimilarItems(similar || []); })
                    .catch(() => { if (!controller.signal.aborted) setSimilarItems([]); })
                    .finally(() => { if (!controller.signal.aborted) setSimilarLoading(false); });
            })
            .catch(err => {
                if (!controller.signal.aborted) {
                    console.error('Detail fetch error:', err);
                    setError(true);
                }
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });

        return () => controller.abort();
    }, [itemId]);

    // Escape 닫기, 포커스 가두기/복원으로 키보드 사용자도 모달을 벗어나지 않게 한다.
    useEffect(() => {
        if (!itemId) return undefined;
        const previousFocus = document.activeElement;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        const focusTimer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
        const onKeyDown = (event) => {
            if (event.defaultPrevented) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                onCloseRef.current();
                return;
            }
            if (event.key !== 'Tab' || !dialogRef.current) return;
            const focusable = [...dialogRef.current.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            )].filter(node => node.tabIndex >= 0 && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden');
            if (!focusable.length) {
                event.preventDefault();
                dialogRef.current.focus();
                return;
            }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (!dialogRef.current.contains(document.activeElement)) {
                event.preventDefault();
                (event.shiftKey ? last : first).focus();
            } else if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        };
        document.addEventListener('keydown', onKeyDown);
        return () => {
            window.clearTimeout(focusTimer);
            document.removeEventListener('keydown', onKeyDown);
            document.body.style.overflow = previousOverflow;
            if (previousFocus instanceof HTMLElement) previousFocus.focus();
        };
    }, [itemId]);

    if (!itemId) return null;

    // ai_data를 안전하게 파싱 (백엔드가 객체 또는 JSON 문자열 둘 다 반환 가능)
    let ai = {};
    if (item) {
        try {
            if (typeof item.ai_data === 'string' && item.ai_data.trim() && item.ai_data !== 'null') {
                ai = JSON.parse(item.ai_data);
            } else if (typeof item.ai_data === 'object' && item.ai_data !== null) {
                ai = item.ai_data;
            }
        } catch (e) {
            console.error('ItemDetailModal: ai_data 파싱 실패', e);
        }
    }

    const PropertyRow = ({ label, value, color }) => {
        if (!value || value === '-') return null;
        return (
            <div className="flex border-b border-gray-100 dark:border-gray-700 min-h-[36px]">
                <span className="w-36 px-3 py-2 text-sm text-gray-500 dark:text-gray-400 flex-shrink-0 font-medium">{label}</span>
                <span className={`flex-1 px-3 py-2 text-sm ${color || 'text-gray-800 dark:text-gray-200'} flex items-center flex-wrap gap-1`}>
                    {Array.isArray(value) ? value.map((v, i) => (
                        <span key={i} className={tone === 'news' ? 'inline-block px-2 py-0.5 bg-teal-50 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300 rounded text-xs font-medium' : 'inline-block px-2 py-0.5 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded text-xs font-medium'}>{v}</span>
                    )) : value}
                </span>
            </div>
        );
    };

    const getVal = (aiKey, itemKey) => ai[aiKey] || item?.[itemKey] || '';
    const newsPresentation = storedNewsPresentation(item);

    const parseList = (val) => {
        if (!val) return [];
        if (Array.isArray(val)) return val;
        return val.split(',').map(s => s.trim()).filter(Boolean);
    };

    return (
        <AnimatePresence>
            <motion.div
                className="fixed inset-0 z-50 flex items-center justify-center p-4"
                initial={reduceMotion ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
            >
                {/* Backdrop */}
                <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} aria-hidden="true" />

                {/* Modal */}
                <motion.div
                    ref={dialogRef}
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby={!loading ? titleId : undefined}
                    aria-label={loading ? '상세 정보를 불러오는 중' : undefined}
                    tabIndex={-1}
                    className="relative flex flex-col bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-2xl max-h-[85dvh] overflow-hidden"
                    initial={reduceMotion ? false : { scale: 0.95, y: 20 }}
                    animate={{ scale: 1, y: 0 }}
                    exit={reduceMotion ? {} : { scale: 0.95, y: 20 }}
                >
                    {/* Header */}
                    <div className="flex shrink-0 items-start justify-between p-5 border-b border-gray-100 dark:border-gray-700 max-h-[30dvh] overflow-y-auto">
                        <div className="flex-1 min-w-0 pr-4">
                            {loading ? (
                                <div className="h-6 w-3/4 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
                            ) : (
                                <>
                                    <div className="flex items-center gap-2 mb-2">
                                        <span className={tone === 'news' ? 'text-xs px-2 py-0.5 bg-teal-100 dark:bg-teal-900/40 text-teal-700 dark:text-teal-300 rounded font-medium' : 'text-xs px-2 py-0.5 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 rounded font-medium'}>
                                            {item?.category || '기타'}
                                        </span>
                                        <span className="text-xs text-gray-400">{item?.site_name}</span>
                                    </div>
                                    <h2 id={titleId} className="text-lg font-bold text-gray-800 dark:text-gray-100 leading-tight break-words">
                                        {error ? '상세 정보를 불러오지 못했습니다' : item?.title || '상세 정보'}
                                    </h2>
                                </>
                            )}
                        </div>
                        <button ref={closeButtonRef} type="button" onClick={onClose} aria-label="상세 정보 닫기" className={tone === 'news' ? 'flex h-11 w-11 shrink-0 items-center justify-center hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-500' : 'flex h-11 w-11 shrink-0 items-center justify-center hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500'}>
                            <X className="w-5 h-5 text-gray-400" aria-hidden="true" />
                        </button>
                    </div>

                    {/* Content */}
                    <div data-news-detail-body className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                        {loading ? (
                            <div className="flex items-center justify-center py-16">
                                <Loader2 className={tone === 'news' ? 'w-8 h-8 animate-spin text-teal-500' : 'w-8 h-8 animate-spin text-blue-500'} />
                            </div>
                        ) : error ? (
                            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
                                <AlertTriangle className="w-10 h-10 mb-3 text-red-400" />
                                <p className="text-sm font-medium text-gray-600 dark:text-gray-300">상세 정보를 불러오지 못했습니다.</p>
                                <p className="text-xs mt-1">잠시 후 다시 시도해주세요.</p>
                            </div>
                        ) : (
                            <>
                                {(ai?.source_attachments?.status === 'review_required' || ai?.source_page?.status === 'review_required' || ['partial', 'empty'].includes(ai?.source_input?.status)) && (
                                    <div className="m-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-300">
                                        <p className="font-semibold">원문 확인이 필요해요</p>
                                        {ai?.source_page?.status === 'review_required' && <p className="mt-1 break-words">{ai.source_page.note || '페이지의 일부 내용을 가져오지 못했습니다.'}</p>}
                                        {ai?.source_attachments?.status === 'review_required' && <p className="mt-1 break-words">{ai.source_attachments.note || '일부 첨부파일을 읽지 못했습니다.'}</p>}
                                        {['partial', 'empty'].includes(ai?.source_input?.status) && <p className="mt-1 break-words">{ai.source_input.note || 'AI가 전체 내용을 확인하지 못했습니다.'}</p>}
                                        <p className="mt-1 text-xs">아래 요약에 빠진 내용이 있을 수 있으니 원문과 첨부파일을 확인해주세요.</p>
                                    </div>
                                )}
                                {/* Properties */}
                                <div className="border-b border-gray-100 dark:border-gray-700">
                                    <PropertyRow label="대상 특성" value={parseList(getVal('target_characteristics', 'target_characteristics'))} />
                                    <PropertyRow label="대상" value={getVal('target', 'target')} />
                                    <PropertyRow label="연령" value={
                                        (getVal('start_age_months', 'start_age_months') && getVal('end_age_months', 'end_age_months'))
                                            ? `${getVal('start_age_months', 'start_age_months')}~${getVal('end_age_months', 'end_age_months')}개월`
                                            : getVal('age_detail', 'age_detail')
                                    } />
                                    <PropertyRow label="소득 수준" value={getVal('target_income', 'target_income')} />
                                    <PropertyRow label="거주지" value={parseList(getVal('residence', 'residence'))} />
                                    <PropertyRow label="생애주기" value={parseList(getVal('life_cycle', 'life_cycle'))} />
                                    <PropertyRow label="마감일" value={item?.deadline} color="text-red-500" />
                                    <PropertyRow label="일정" value={item?.event_date} />
                                    <PropertyRow label="문의처" value={getVal('contact', 'contact')} />
                                    <PropertyRow label="자격 요건" value={getVal('eligibility_detail', 'eligibility_detail')} />
                                    <PropertyRow label="매칭 키워드" value={item?.matched_keyword} />
                                    <PropertyRow label="수집일" value={item?.scraped_at ? new Date(item.scraped_at).toLocaleString('ko-KR') : ''} />
                                    {item?.change_log && <PropertyRow label="변경 이력" value={item.change_log} color="text-orange-500" />}
                                </div>

                                {/* Support Detail */}
                                <NewsSupportContent
                                    supportDetail={newsPresentation ? newsPresentation.support_detail : getVal('support_detail', 'support_detail')}
                                    presentationKind={newsPresentation?.presentation_kind}
                                    sourceNote={newsPresentation?.source_note}/>

                                {/* 변경 내역 Diff */}
                                {item?.change_log && <ChangeDiffView itemId={itemId} />}

                                {/* Similar Items */}
                                {(similarLoading || similarItems.length > 0) && (
                                    <div className="p-5 border-t border-gray-100 dark:border-gray-700">
                                        <h3 className="text-sm font-bold text-gray-700 dark:text-gray-300 mb-3">관련 항목</h3>
                                        {similarLoading ? (
                                            <div className="space-y-2">
                                                {[1, 2, 3].map(i => (
                                                    <div key={i} className="h-9 bg-gray-100 dark:bg-gray-700 rounded-lg animate-pulse" />
                                                ))}
                                            </div>
                                        ) : (
                                            <div className="space-y-1">
                                                {similarItems.map((si, i) => {
                                                    const Tag = isRealLink(si.link) ? 'a' : 'div';
                                                    const linkProps = isRealLink(si.link)
                                                        ? { href: si.link, target: '_blank', rel: 'noopener noreferrer' }
                                                        : {};
                                                    return (
                                                    <Tag
                                                        key={si.id ?? i}
                                                        {...linkProps}
                                                        className="flex items-center gap-2 px-2 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 group transition-colors"
                                                    >
                                                        <span className={tone === 'news' ? 'text-[10px] px-1.5 py-0.5 bg-teal-50 dark:bg-teal-900/30 text-teal-600 dark:text-teal-400 rounded font-medium flex-shrink-0' : 'text-[10px] px-1.5 py-0.5 bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded font-medium flex-shrink-0'}>
                                                            {si.category || '기타'}
                                                        </span>
                                                        <span className={tone === 'news' ? 'text-sm text-gray-700 dark:text-gray-300 group-hover:text-teal-600 transition-colors truncate' : 'text-sm text-gray-700 dark:text-gray-300 group-hover:text-blue-600 transition-colors truncate'}>
                                                            {si.title}
                                                        </span>
                                                        <ExternalLink className="w-3 h-3 text-gray-300 dark:text-gray-600 flex-shrink-0 ml-auto" />
                                                    </Tag>
                                                    );
                                                })}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </>
                        )}
                    </div>

                    {/* Footer */}
                    {!loading && isRealLink(item?.link) && (
                        <div className="shrink-0 p-4 border-t border-gray-100 dark:border-gray-700 flex justify-end">
                            <a
                                href={item.link}
                                target="_blank"
                                rel="noopener noreferrer"
                                className={tone === 'news' ? 'inline-flex items-center gap-2 px-4 py-2 bg-teal-700 text-white rounded-lg hover:bg-teal-800 transition-colors text-sm font-medium' : 'inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors text-sm font-medium'}
                            >
                                <ExternalLink className="w-4 h-4" />
                                원문 보기
                            </a>
                        </div>
                    )}
                </motion.div>
            </motion.div>
        </AnimatePresence>
    );
};

export default ItemDetailModal;

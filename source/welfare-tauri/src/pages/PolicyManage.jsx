import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import {
    RefreshCcwDot, RefreshCw, ExternalLink, Loader2, Copy, AlertTriangle,
    CheckCircle2, Link2Off, Sparkles, FilePlus2, Circle, ScanSearch, FileText, ChevronDown, ChevronUp,
    X, Building2, Tag, Target, Link2
} from 'lucide-react';
import PolicyWatch from './PolicyWatch';
import PolicyUndoDialog from '../components/PolicyUndoDialog';
import PolicyReviewQueue from '../components/PolicyReviewQueue';
import { formatStoredUtcTime } from '../utils/dateTime';
import { Tooltip } from '../components/ui/Tooltip';
import { useAiStatus } from '../hooks/useAiStatus';
import { startStatusPolling } from '../utils/statusPolling';
import { selectedVisibleFields, allVisibleSelected, isInvalidProposal, filterPolicyChanges, groupPolicyFields } from '../utils/policySelection';
import { applyWithSourceConfirmation } from '../utils/policyReviewConfirmation';
import { restyleNeedsReview } from '../utils/policyRestyleOutcome';
import { policyNeedsReview, policyReviewNotes, policyReviewIsMirrored } from '../utils/policyReviewOutcome';
import { policyFullCheckConfirmation, policyFullCheckPhase, policyFullCheckNeedsAttention, policyFullCheckHasResult } from '../utils/policyFullCheck';
import { policyCanApply, policyStateLabel, policyCardReviewNotes, readPolicyChanges, policyWriteNeedsRecovery, policyRecoveryRequired } from '../utils/policyReviewState';
import { draftSourceReviewItems, emptyDraftSourceChecks, updateDraftSourceCheck, draftSourceConfirmed, draftSourceReview, draftIncomeReviewRequired, draftSourceCoverageNotice, draftAgeRangeError } from '../utils/policyDraftSourceReview';

const PolicyReviewDetails = ({ status }) => {
    const notes = policyReviewNotes(status);
    if (!notes.length) return null;
    return <details className="mt-2 text-sm text-amber-800 dark:text-amber-200 break-words">
        <summary className="cursor-pointer font-medium">확인이 필요한 정책 {status.review_count || notes.length}건</summary>
        <p className="mt-2">보류된 정책의 기존 내용과 제안은 유지됩니다. 보류는 적용 완료가 아니며, 아래 사유에 따라 확인이 필요합니다.</p>
        <ul className="mt-2 space-y-2">
            {notes.map((note, index) => <li key={index} className="border-l-2 border-amber-200 pl-3">
                <p className="font-medium">{note.title}</p><p>{note.note}</p>
                <p className="mt-1 text-gray-600 dark:text-gray-300"><span className="font-medium">다음 단계: </span>{note.nextStep}</p>
            </li>)}
        </ul>
        {status.review_count > notes.length && <p className="mt-2">처음 {notes.length}건의 사유를 표시합니다.</p>}
    </details>;
};

const usePolicyTaskPolling = (active, fetchStatus, setStatus, setBusy, reload) => {
    useEffect(() => {
        if (!active) return undefined;
        return startStatusPolling({
            fetchStatus,
            onStatus: status => setStatus({ ...status, connectionError: false }),
            onError: () => setStatus(previous => ({ ...previous, connectionError: true })),
            onComplete: () => {
                setBusy(false);
                reload();
            },
        });
    }, [active, fetchStatus, setStatus, setBusy, reload]);
};

const copy = (text) => {
    navigator.clipboard?.writeText(text || '')
        .then(() => toast.success('복사됨'))
        .catch(() => toast.error('복사 실패'));
};

// (V3) 단어(공백 기준) 단위 LCS diff — 바뀐 부분만 색으로 강조하기 위한 세그먼트 목록 생성.
// 텍스트가 너무 길면(토큰 과다) 성능 보호를 위해 diff를 건너뛰고 null 반환(호출자가 원문 그대로 표시).
const wordDiff = (oldText, newText) => {
    const a = (oldText || '').split(/(\s+)/);
    const b = (newText || '').split(/(\s+)/);
    if (a.length > 800 || b.length > 800) return null;
    const n = a.length, m = b.length;
    const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
            dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
    }
    const segs = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
        if (a[i] === b[j]) { segs.push({ type: 'equal', value: a[i] }); i++; j++; }
        else if (dp[i + 1][j] >= dp[i][j + 1]) { segs.push({ type: 'del', value: a[i] }); i++; }
        else { segs.push({ type: 'add', value: b[j] }); j++; }
    }
    while (i < n) { segs.push({ type: 'del', value: a[i++] }); }
    while (j < m) { segs.push({ type: 'add', value: b[j++] }); }
    return segs;
};

const DiffOldLine = ({ segs }) => (
    <>{segs.filter(s => s.type !== 'add').map((s, i) => s.type === 'del'
        ? <span key={i} className="bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">{s.value}</span>
        : <span key={i}>{s.value}</span>)}</>
);
const DiffNewLine = ({ segs }) => (
    <>{segs.filter(s => s.type !== 'del').map((s, i) => s.type === 'add'
        ? <span key={i} className="bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300 font-semibold">{s.value}</span>
        : <span key={i}>{s.value}</span>)}</>
);

const FIELD_LEVEL_META = {
    high: { border: 'border-orange-200 bg-orange-50/40 dark:border-orange-800 dark:bg-orange-950/20', text: 'text-orange-900 dark:text-orange-200' },
    info: { border: 'border-sky-200 bg-sky-50/40 dark:border-sky-800 dark:bg-sky-950/20', text: 'text-sky-900 dark:text-sky-200' },
    low: { border: 'border-gray-100 bg-gray-50/50 dark:border-gray-700 dark:bg-gray-800/30', text: 'text-gray-500' },
};

// (U1) Notion 자동 반영 지원 필드 — 백엔드 _LABEL_TO_FIELD와 동일 목록.
// "지원금액·기간"은 실제 Notion 속성이 없는 파생 비교 신호라 제외(복사해서 수동 반영).
// (2026-07-20) 라벨을 실제 Notion 속성명으로 개편 — 구 축약 라벨은 개편 전에 저장된
// 진단 카드(재계산 전)도 계속 적용 가능하도록 함께 유지한다.
const APPLYABLE_FIELDS = new Set([
    '거주지', '생애주기', '대상 특성', '분류', '세부 연령', '추가 자격요건', '대상 소득 수준', '상세 지원 내용',
    '대상특성', '대상연령', '자격요건', '대상소득', '상세지원내용',
]);

// new(제안값)이 사실상 빈 값이면 "Notion에 적용"을 눌러도 반영할 게 없어 내부 검증에서
// 막히기만 하고 사용자에게는 의미불명 에러로 보인다 — 애초에 "원문에서 못 찾음"으로 안내한다.
const isEmptyValue = isInvalidProposal;

const FieldDiff = ({ f, onApply, onUndo, checked, onToggleCheck, cardBusy = false, applyReady = false }) => {
    const meta = FIELD_LEVEL_META[f.level] || FIELD_LEVEL_META.low;
    const display = f.styled || f.new;
    const [applying, setApplying] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const newIsEmpty = isEmptyValue(display);
    const canApply = applyReady && APPLYABLE_FIELDS.has(f.field) && !!onApply && !newIsEmpty;
    // (2026-08-28, GPT Codex 지적 ①) 백엔드에 정책별 락을 걸어도, 같은 카드의 다른 필드를
    // 사용자가 그새 눌러버리면 대기만 할 뿐 "누른 줄도 모르고 또 누르는" 혼란은 남는다.
    // 같은 카드에서 다른 필드가 적용 중이면 이 필드의 버튼도 함께 잠가 UX로도 막는다.
    const isDisabled = applying || cardBusy;
    const needsExpansion = f.note === 'bloat' || String(f.old || '').length + String(display || '').length > 1200;
    const isBloat = needsExpansion && !expanded;
    // Do not run the word-diff matrix for collapsed long text.
    const segs = useMemo(() => (!isBloat && f.old && display) ? wordDiff(f.old, display) : null, [f.old, display, isBloat]);
    const preview = value => String(value || '(없음)').slice(0, 240) + (String(value || '').length > 240 ? '…' : '');
    useEffect(() => setExpanded(false), [f.old, display]);

    const handleApply = async () => {
        setApplying(true);
        try { await onApply(f.field); }
        finally { setApplying(false); }
    };

    if (newIsEmpty && !f.applied) return null;

    return (
        <div className={`rounded-xl border p-4 shadow-sm ${f.applied ? 'border-gray-100 bg-gray-50/50 dark:border-gray-700 dark:bg-gray-800/30' : meta.border}`}>
            <div className={`flex flex-wrap items-center gap-2 mb-4 -mx-4 -mt-4 px-4 py-3 rounded-t-xl border-b ${!f.applied && f.level === 'high' ? 'bg-orange-100/60 border-orange-200 dark:bg-orange-950/40 dark:border-orange-800' : !f.applied && f.level === 'info' ? 'bg-sky-100/60 border-sky-200 dark:bg-sky-950/40 dark:border-sky-800' : 'bg-gray-50 border-gray-100 dark:bg-gray-800 dark:border-gray-700'}`}>
                <h3 className={`text-base font-semibold ${f.applied ? 'text-gray-500' : meta.text}`}>
                    {f.field}
                </h3>
                {!f.applied && <span className={`text-xs font-medium ${f.level === 'high' ? 'text-amber-800 dark:text-amber-300' : f.level === 'info' ? 'text-sky-700 dark:text-sky-300' : 'text-gray-600 dark:text-gray-300'}`}>{f.level === 'high' ? '● 주요' : f.level === 'info' ? '● 보강' : '○ 사소'}</span>}
                {f.applied && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300 flex items-center gap-0.5">
                        <CheckCircle2 size={10} /> {f.needs_verification ? '반영 상태 확인 필요' : 'Notion 적용 완료'}
                    </span>
                )}
                {f.styled && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-300 flex items-center gap-0.5">
                        <Sparkles size={10} /> 문장 다듬기 제안
                    </span>
                )}
                <Tooltip content="새 값 복사" wrapperClassName="ml-auto">
                    <button disabled={newIsEmpty} aria-label={`${f.field} 제안 내용 복사`} onClick={() => copy(display)}
                        className="text-gray-400 hover:text-emerald-500"><Copy size={13} /></button>
                </Tooltip>
            </div>
            {f.note === 'sparse' && (
                <div className="mb-1.5 text-[11px] text-amber-600 dark:text-amber-400 flex items-start gap-1">
                    <AlertTriangle size={11} className="mt-0.5 flex-shrink-0" />
                    <span className="break-keep">섹션 구성이 기존과 달라 자동 정리되지 않았습니다 — 원문 그대로입니다. "스타일 제안 생성"으로 기존 섹션에 맞게 재정리할 수 있어요.</span>
                </div>
            )}
            {f.note === 'narrowing' && (
                <div className="mb-1.5 text-[11px] text-amber-600 dark:text-amber-400 flex items-start gap-1">
                    <AlertTriangle size={11} className="mt-0.5 flex-shrink-0" />
                    <span className="break-keep">제안이 기존 태그의 일부만 담고 있습니다 — 출처 1건만 본 결과라 좁게 나올 수 있어요. 기존 값 유지를 권장합니다.</span>
                </div>
            )}
            {f.consistency_note && (
                <div className="mb-1.5 text-[11px] text-amber-600 dark:text-amber-400 flex items-start gap-1">
                    <AlertTriangle size={11} className="mt-0.5 flex-shrink-0" />
                    <span className="break-keep">{f.consistency_note}</span>
                </div>
            )}
            {/* (2026-08-07) id=13 실사고(대상특성 값이 세부 연령에 잘못 들어감) 재발 방지 —
                진단 생성 시점에 이미 의심스러운 값이면 적용 전에 눈에 띄게 경고. 실제
                차단은 apply 시점(SUSPICIOUS_VALUE)에서 한 번 더 일어난다. */}
            {f.validation_warning && (
                <div className="mb-1.5 text-[11px] text-red-600 dark:text-red-400 flex items-start gap-1">
                    <AlertTriangle size={11} className="mt-0.5 flex-shrink-0" />
                    <span className="break-keep">{f.validation_warning}</span>
                </div>
            )}
            <div data-policy-comparison className="grid grid-cols-1 xl:grid-cols-2 gap-4 text-sm">
                <section aria-label="반영 전 Notion 내용" className="min-w-0 py-2">
                    <h4 className="mb-2 font-semibold text-gray-700 dark:text-gray-300">{f.applied ? '반영 전 내용' : f.snapshot_current === true ? '점검 시 Notion 내용' : '이전에 저장된 비교 내용'}{isBloat ? ' · 앞부분' : ''}</h4>
                    <div className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words leading-relaxed">{isBloat ? preview(f.old) : !newIsEmpty && segs ? <DiffOldLine segs={segs} /> : f.old || '(없음)'}</div>
                </section>
                {newIsEmpty ? <p className="text-xs text-gray-500 dark:text-gray-400">추가 제안 없음</p> : <section aria-label="변경 제안" className="min-w-0 border-t border-gray-200 dark:border-gray-700 pt-4 pb-2 xl:border-t-0 xl:border-l xl:pl-5 xl:pt-2">
                    <h4 className="mb-2 font-semibold text-emerald-800 dark:text-emerald-300">{f.applied ? 'Notion에 반영한 내용' : '변경 제안 · 아직 반영하지 않음'}{isBloat ? ' · 앞부분' : ''}</h4>
                    <div className="text-gray-800 dark:text-gray-100 whitespace-pre-wrap break-words leading-relaxed">{newIsEmpty ? '제안값을 확인할 수 없습니다. 기존 Notion 내용은 유지됩니다.' : isBloat ? preview(display) : segs ? <DiffNewLine segs={segs} /> : display}</div>
                </section>}
                {needsExpansion && (
                    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}
                        className="col-span-full text-xs text-indigo-600 dark:text-indigo-300 hover:underline text-left flex items-center gap-1">
                        {expanded ? <><ChevronUp size={11} /> 전체 비교 접기</> : <><ChevronDown size={11} /> 현재 내용과 변경 제안 전체 보기</>}
                    </button>
                )}
                {!isBloat && f.styled && f.styled !== f.new && (
                    <div className="col-span-full text-gray-500 text-xs break-keep">원본 추출: {f.new || '(없음)'}</div>
                )}
            </div>
            {newIsEmpty && (
                <p className="mt-1 text-[10px] text-gray-400">필요하면 "원문 비교 보기"에서 실제 출처를 확인해 Notion에서 직접 입력해주세요.</p>
            )}
            {canApply && !f.applied && f.requires_individual_review && (
                <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">원문 대조 필요 · 임시 선택 후에도 최종 적용 전에 이 항목의 조건과 혜택을 확인합니다.</p>
            )}
            {f.applied && <div className="mt-3">
                {f.needs_verification && <p className="mb-2 text-xs text-amber-700">원격 반영 후 저장 상태를 확인하지 못했습니다. 다시 적용하지 말고 Notion의 현재 내용과 복구 기록을 확인해주세요.</p>}
                {f.undo_id ? <><button type="button" onClick={() => onUndo?.(f)} disabled={isDisabled || f.undo_available === false} className="min-h-10 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">적용 취소</button>{f.undo_available === false && <p className="mt-2 text-xs text-amber-700">{f.guidance || 'Notion에서 현재 반영 결과를 확인해주세요. 불확실한 요청은 다시 보내지 않습니다.'}</p>}</>
                    : <p className="text-xs text-gray-500">이전 버전에서 적용한 항목은 취소용 원본이 없어 Notion에서 직접 수정해주세요.</p>}
            </div>}
            {canApply && !f.applied && (
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <Tooltip content={cardBusy && !applying ? "이 정책의 다른 변경 사항을 적용하는 중입니다 — 완료 후 다시 시도해주세요" : "이 필드만 Notion 페이지에 바로 반영"}>
                        <button onClick={handleApply} disabled={isDisabled}
                            className="text-sm px-3 py-2 rounded-lg border border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-50 dark:hover:bg-emerald-950/20 disabled:opacity-50 flex items-center gap-1">
                            {applying ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 size={12} />}
                            바로 적용
                        </button>
                    </Tooltip>
                    {onToggleCheck && (
                        <Tooltip content="선택한 뒤 화면 아래 작업바에서 한 번에 Notion에 반영할 수 있습니다. 선택만으로는 반영되지 않습니다.">
                            <button type="button" aria-pressed={checked} onClick={onToggleCheck} disabled={isDisabled}
                                className={`text-sm px-3 py-2 rounded-lg border flex items-center gap-1 transition-colors disabled:opacity-50 ${checked
                                    ? 'bg-gray-600 border-gray-600 text-white'
                                    : 'border-gray-300 dark:border-gray-600 text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                                {checked ? <CheckCircle2 size={12} /> : <Circle size={12} />}
                                {checked ? '반영할 내용 선택됨' : '반영할 내용 선택'}
                            </button>
                        </Tooltip>
                    )}
                </div>
            )}
        </div>
    );
};

const SourceDiffView = ({ sd }) => {
    const [open, setOpen] = useState(false);
    if (!sd || !sd.new) return null;
    return (
        <div className="mt-2">
            <button onClick={() => setOpen(o => !o)}
                className="text-xs text-gray-400 hover:text-indigo-500 flex items-center gap-1">
                <FileText size={13} /> 원문 비교 보기
                {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
            {open && (
                <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                    {sd.old ? (
                        <div className="rounded-lg border border-gray-100 dark:border-gray-700 p-2 bg-gray-50/50 dark:bg-gray-800/30 max-h-48 overflow-y-auto">
                            <div className="text-gray-400 font-semibold mb-1">이전 원문</div>
                            <div className="whitespace-pre-wrap break-keep text-gray-600 dark:text-gray-300">{sd.old}</div>
                        </div>
                    ) : (
                        <div className="rounded-lg border border-gray-100 dark:border-gray-700 p-2 bg-gray-50/50 dark:bg-gray-800/30 text-gray-400 flex items-center justify-center">
                            (최초 감사 — 비교할 이전 원문 없음)
                        </div>
                    )}
                    <div className="rounded-lg border border-indigo-100 dark:border-indigo-900/40 p-2 bg-indigo-50/30 dark:bg-indigo-950/10 max-h-48 overflow-y-auto">
                        <div className="text-indigo-400 font-semibold mb-1">현재 원문</div>
                        <div className="whitespace-pre-wrap break-keep text-gray-700 dark:text-gray-200">{sd.new}</div>
                    </div>
                </div>
            )}
        </div>
    );
};

const HealthToggleList = ({ icon, label, items, openLink }) => {
    const [open, setOpen] = useState(false);
    if (items.length === 0) return null;
    return (
        <div>
            <button onClick={() => setOpen(o => !o)}
                className="flex items-center gap-1.5 font-medium hover:underline">
                {icon} {label} {items.length}건
                {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
            {open && (
                <ul className="text-xs space-y-0.5 pl-5 list-disc mt-1.5">
                    {items.map(p => (
                        <li key={p.id}>
                            {p.title}
                            {p.deeplink && (
                                <button onClick={() => openLink(p.deeplink)}
                                    className="ml-1.5 text-indigo-400 hover:text-indigo-600 inline-flex items-center"><ExternalLink size={11} /></button>
                            )}
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
};

const INTENSITY_META = {
    high: { label: '주요 변경 제안', badge: 'bg-amber-50 text-amber-800 dark:bg-amber-950/20 dark:text-amber-200', border: 'border-gray-200 dark:border-gray-700' },
    info: { label: '보강 후보', badge: 'bg-sky-50 text-sky-700 dark:bg-sky-950/20 dark:text-sky-200', border: 'border-gray-200 dark:border-gray-700' },
    low: { label: '사소 변경', badge: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300', border: 'border-gray-100 dark:border-gray-700' },
};

const URL_DRAFT_MULTI_FIELDS = [
    ['residence', '거주지'],
    ['target_characteristics', '대상 특성'],
    ['life_cycle', '생애주기'],
];

const URL_DRAFT_TEXT_FIELDS = [
    ['age_detail', '세부 연령', 2],
    ['target_income', '대상 소득 수준', 2],
    ['eligibility_detail', '추가 자격요건', 3],
    ['support_detail', '상세 지원 내용', 8],
    ['contact', '문의처', 2],
];

const URL_DRAFT_REQUIRED_LABELS = {
    title: '사업명', category: '분류', residence: '거주지',
    target_characteristics: '대상 특성', life_cycle: '생애주기',
    start_age_months: '시작 월령(개월)', end_age_months: '종료 월령(개월)',
    age_detail: '세부 연령', target_income: '대상 소득 수준',
    eligibility_detail: '추가 자격요건', support_detail: '상세 지원 내용', contact: '문의처',
};

const PolicyManage = () => {
    const { data: aiStatus } = useAiStatus();
    const queryClient = useQueryClient();
    // (2026-07-20) 칩 이름·개수는 하드코딩된 POLICY_DBS 대신, 실제 설정된 DB 목록(이름은
    // Notion에서 동기화한 진짜 제목)을 서버에서 받아와 표시한다. 요청 전 잠깐만 과거
    // 기본값을 쓰고, 설정이 비어 있으면 가짜 보관함을 만들지 않고 설정 안내를 보여준다.
    const dbsQ = useQuery({ queryKey: ['policyDbs'], queryFn: welfareApi.getPolicyDbs });
    const displayDbs = dbsQ.data?.dbs || [];
    const [selected, setSelected] = useState(new Set());
    const [busy, setBusy] = useState(false);
    const [status, setStatus] = useState(null);
    const [syncBusy, setSyncBusy] = useState(false);
    const [syncStatus, setSyncStatus] = useState(null);
    const [auditBusy, setAuditBusy] = useState(false);
    const [auditStatus, setAuditStatus] = useState(null);
    const [recomputeBusy, setRecomputeBusy] = useState(false);
    const [recomputeStatus, setRecomputeStatus] = useState(null);
    const [restyleBusy, setRestyleBusy] = useState(false);
    const [undoRequest, setUndoRequest] = useState(null);
    const [restyleStatus, setRestyleStatus] = useState(null);
    // (B1) "한 번에 점검" 통합 액션
    const [oneClickBusy, setOneClickBusy] = useState(false);
    const [oneClickPreparing, setOneClickPreparing] = useState(false);
    const oneClickRequest = useRef(false);
    const [oneClickStatus, setOneClickStatus] = useState(null);
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [draftPick, setDraftPick] = useState({}); // itemId → dbKey
    const [dismissingCandidateIds, setDismissingCandidateIds] = useState(new Set());
    const dismissingCandidateRef = useRef(new Set());
    const [urlDraftOpen, setUrlDraftOpen] = useState(false);
    const [urlDraftInput, setUrlDraftInput] = useState({ url: '', title: '', dbKey: '' });
    const [urlDraftPrivacyReviewed, setUrlDraftPrivacyReviewed] = useState(false);
    const [urlDraftDocumentDetected, setUrlDraftDocumentDetected] = useState(false);
    const urlDraftIsDocument = urlDraftDocumentDetected || /\.(?:pdf|png|jpe?g|webp)(?:[?#]|$)/i.test(urlDraftInput.url.trim());
    useEffect(() => {
        setUrlDraftPrivacyReviewed(false);
        setUrlDraftDocumentDetected(false);
    }, [urlDraftInput.url]);
    const [urlDraftPreview, setUrlDraftPreview] = useState(null);
    const [urlDraftCreated, setUrlDraftCreated] = useState(null);
    const [urlDraftPreviewFailed,setUrlDraftPreviewFailed]=useState(false);
    const [urlDraftSourceChecks, setUrlDraftSourceChecks] = useState(emptyDraftSourceChecks);
    const urlDraftSourceChecksRef = useRef(urlDraftSourceChecks);
    const resetUrlDraftSourceReview = useCallback(() => {
        const next = emptyDraftSourceChecks();
        // Invalidate synchronously too: an edit followed by a click cannot reuse an old render's consent.
        urlDraftSourceChecksRef.current = next;
        setUrlDraftSourceChecks(next);
    }, []);
    const clearUrlDraftPreview = () => {
        resetUrlDraftSourceReview();
        setUrlDraftPreview(null);
    };
    const urlDraftSourceReview = useMemo(() => draftSourceReview(urlDraftPreview?.source_review), [urlDraftPreview?.source_review]);
    const urlDraftSourceConfirmed = draftSourceConfirmed(urlDraftSourceChecks);
    const urlDraftAgeError=draftAgeRangeError(urlDraftPreview?.fields);
    const urlDraftCoverageNotice=draftSourceCoverageNotice(urlDraftPreview?.source_review);
    const [urlDraftBusy, setUrlDraftBusy] = useState(false);
    const [urlDraftCreateBusy, setUrlDraftCreateBusy] = useState(false);
    const [urlDraftStatus, setUrlDraftStatus] = useState(null);
    const [urlDraftStatusBusy, setUrlDraftStatusBusy] = useState(false);
    const urlDraftOperation = useRef(null), urlDraftGeneration = useRef(0), urlDraftMounted = useRef(true);
    const urlDraftPreviewGeneration = useRef(null);
    const urlDraftLocked = urlDraftBusy || urlDraftCreateBusy || urlDraftStatusBusy;
    const urlDraftNeedsCheck = Boolean(urlDraftStatus && urlDraftStatus.state !== 'clear');
    useEffect(() => {
        urlDraftMounted.current = true;
        return () => {urlDraftMounted.current = false;urlDraftGeneration.current++;};
    }, []);
    useEffect(() => {
        // A background refresh may remove or replace the selected policy table.
        urlDraftGeneration.current++;
        resetUrlDraftSourceReview();
        setUrlDraftPreview(null);
        setUrlDraftPreviewFailed(false);
    }, [urlDraftInput.dbKey, resetUrlDraftSourceReview]);
    const changeUrlDraftInput = (key, value) => {
        if (['create','retry'].includes(urlDraftOperation.current?.kind)) return;
        urlDraftGeneration.current++;
        clearUrlDraftPreview();
        setUrlDraftStatus(null);
        setUrlDraftPreviewFailed(false);
        if (key === 'url') {setUrlDraftPrivacyReviewed(false);setUrlDraftDocumentDetected(false);}
        setUrlDraftInput(previous => ({...previous,[key]:value}));
    };
    const beginUrlDraftOperation = kind => {
        if (urlDraftOperation.current || !urlDraftMounted.current) return null;
        const operation = {kind,generation:urlDraftGeneration.current};
        urlDraftOperation.current = operation;
        setUrlDraftBusy(kind === 'preview');
        setUrlDraftCreateBusy(kind === 'create');
        setUrlDraftStatusBusy(kind === 'status' || kind === 'retry');
        return operation;
    };
    const currentUrlDraftOperation = operation => urlDraftMounted.current && operation.generation === urlDraftGeneration.current;
    const finishUrlDraftOperation = operation => {
        if (urlDraftOperation.current !== operation) return;
        urlDraftOperation.current = null;
        if (urlDraftMounted.current) {setUrlDraftBusy(false);setUrlDraftCreateBusy(false);setUrlDraftStatusBusy(false);}
    };
    // (B3) 기본은 중요·보강만 — ⚪사소는 대량이라 압도감을 주니 접어서 "N건 보기"로 노출
    const [intensityFilter, setIntensityFilter] = useState('important'); // important|all|high|info|low
    const [checkedIds, setCheckedIds] = useState(new Set());
    const [reviewSelectionOpen, setReviewSelectionOpen] = useState(false);
    const [displayLimit, setDisplayLimit] = useState(20);
    const [expandedPolicies, setExpandedPolicies] = useState(new Set());
    const [pageTab, setPageTab] = useState('main'); // main|watch|candidates
    // (2026-07-20) 필드 단위 일괄 적용 — 이미 사람이 필드별로 검토·체크한 것만 모아서
    // 한 번에 Notion 반영. 키는 "policyId::fieldLabel".
    const [checkedFields, setCheckedFields] = useState(new Set());
    const [bulkApplyBusy, setBulkApplyBusy] = useState(false);
    const [reviewBusy, setReviewBusy] = useState(false);
    const [bulkResult, setBulkResult] = useState(null);
    const [resumeError, setResumeError] = useState(false);
    // (2026-08-28, GPT Codex 지적 ①) 같은 카드의 서로 다른 필드를 빠르게 연달아 "적용"하면
    // 백엔드 락으로 데이터 유실은 막히지만, 뒤 요청은 그냥 대기했다가 처리된다 — 사용자가
    // 뭘 눌렀는지 헷갈리지 않도록 카드 단위로 적용 중임을 표시하고 나머지 버튼도 잠근다.
    const [applyingPolicyIds, setApplyingPolicyIds] = useState(new Set());
    const toggleFieldChecked = (policyId, field) => setCheckedFields(prev => {
        const key = `${policyId}::${field}`;
        const n = new Set(prev);
        n.has(key) ? n.delete(key) : n.add(key);
        return n;
    });

    const dbKeys = [...selected];

    useEffect(() => {
        if (!dbsQ.isSuccess) return;
        const available = displayDbs.map(db => db.key);
        setSelected(previous => {
            const kept = [...previous].filter(key => available.includes(key));
            return new Set(kept.length ? kept : available);
        });
        setUrlDraftInput(previous => ({
            ...previous,
            dbKey: available.includes(previous.dbKey) ? previous.dbKey : (available[0] || ''),
        }));
    }, [dbsQ.isSuccess, dbsQ.data]);

    const changesQ = useQuery({
        queryKey: ['policyChanges', dbKeys],
        queryFn: async () => readPolicyChanges(await welfareApi.getPolicyChanges(dbKeys)),
        enabled: dbKeys.length > 0,
        retry: false,
    });
    const candsQ = useQuery({
        queryKey: ['policyCandidates', dbKeys],
        queryFn: () => welfareApi.getPolicyCandidates(dbKeys),
        enabled: dbKeys.length > 0,
    });
    const healthQ = useQuery({
        queryKey: ['policyHealth', dbKeys],
        queryFn: () => welfareApi.getPolicyHealth(dbKeys),
        enabled: dbKeys.length > 0,
    });
    const allChanges = changesQ.data?.items || [];
    // A local count is not proof that the remote table is empty. Offer both first-use paths.
    const showFirstPolicyHelp = dbsQ.isSuccess && displayDbs.length > 0 &&
        displayDbs.every(db => Number(db.local_count || 0) === 0) &&
        changesQ.isSuccess && allChanges.length === 0;
    const openFirstPolicyDraft = () => {
        setPageTab('candidates');
        setUrlDraftOpen(true);
    };
    // Exclude only records actually represented with the new inline-state contract.
    const inlineReviewIds = changesQ.isSuccess ? allChanges.filter(c => Array.isArray(c.review_notes)).map(c => c.id) : [];
    const [jumpPolicyId, setJumpPolicyId] = useState(null);
    const lowCount = allChanges.filter(c => c.intensity === 'low' && c.fields.some(f => !f.applied && !isInvalidProposal(f.styled || f.new))).length;
    const changes = filterPolicyChanges(allChanges, intensityFilter);
    useEffect(() => {
        if (jumpPolicyId === null) return;
        if (changesQ.isFetching) return;
        if (intensityFilter !== 'all') { setIntensityFilter('all'); return; }
        const index = changes.findIndex(policy => policy.id === jumpPolicyId);
        if (index < 0) {
            toast.info('현재 목록에 없는 정책입니다. 보관함 선택이나 처리 완료 여부를 확인해주세요.');
            setJumpPolicyId(null);
            return;
        }
        if (displayLimit <= index) { setDisplayLimit(Math.ceil((index + 1) / 20) * 20); return; }
        if (!expandedPolicies.has(jumpPolicyId)) {
            setExpandedPolicies(previous => new Set(previous).add(jumpPolicyId));
            return;
        }
        const frame = requestAnimationFrame(() => {
            const card = document.getElementById(`policy-review-${jumpPolicyId}`);
            if (card) {
                card.focus({ preventScroll: true });
                card.scrollIntoView({ block: 'start', behavior: 'auto' });
                setJumpPolicyId(null);
            }
        });
        return () => cancelAnimationFrame(frame);
    }, [jumpPolicyId, intensityFilter, displayLimit, changes, changesQ.isFetching, expandedPolicies]);
    const candidates = candsQ.data?.items || [];
    const deadLinkPolicies = healthQ.data?.dead_links || [];
    const noSourcePolicies = healthQ.data?.no_source || [];
    const scopeKey = [...selected].sort().join('|') + ':' + intensityFilter;
    useEffect(() => {
        setCheckedIds(new Set());
        setCheckedFields(new Set());
        setDisplayLimit(20);
    }, [scopeKey]);
    const visibleFields = selectedVisibleFields(changes, checkedFields, true);
    const reviewableChanges = changes.filter(item => (item.fields || []).some(field => !field.applied));
    const visibleIds = reviewableChanges.filter(item => checkedIds.has(item.id)).map(item => item.id);
    const actionBusy = bulkApplyBusy || reviewBusy || applyingPolicyIds.size > 0 || busy || syncBusy ||
        oneClickBusy || oneClickPreparing || auditBusy || recomputeBusy || restyleBusy || changesQ.isFetching || changesQ.isError;
    const selectionBusy = bulkApplyBusy || reviewBusy || applyingPolicyIds.size > 0 || oneClickPreparing;

    const reload = useCallback(({ preserveFields = false } = {}) => {
        queryClient.invalidateQueries({ queryKey: ['policyDbs'] });
        queryClient.invalidateQueries({ queryKey: ['policyChanges'] });
        queryClient.invalidateQueries({ queryKey: ['policyCandidates'] });
        queryClient.invalidateQueries({ queryKey: ['policyHealth'] });
        queryClient.invalidateQueries({ queryKey: ['policyReviews'] });
        setCheckedIds(new Set());
        if (!preserveFields) setCheckedFields(new Set());
    }, [queryClient]);

    const toggleChecked = (id) => setCheckedIds(prev => {
        const n = new Set(prev);
        n.has(id) ? n.delete(id) : n.add(id);
        return n;
    });
    const toggleCheckAll = () => setCheckedIds(prev =>
        allVisibleSelected(reviewableChanges.slice(0, displayLimit), prev) ? new Set() : new Set(reviewableChanges.slice(0, displayLimit).map(c => c.id)));

    const handleBulkConfirm = async () => {
        if (!visibleIds.length || actionBusy) return;
        if (!window.confirm(`선택한 정책 ${visibleIds.length}건을 반영하지 않고 검토 종료할까요?\nNotion 내용은 바뀌지 않으며, 변경 제안은 검토 목록에서 사라집니다.`)) return;
        setReviewBusy(true);
        const t = toast.loading(`${checkedIds.size}건 처리 완료 처리 중...`);
        try {
            await welfareApi.confirmPolicyChangesBulk(visibleIds);
            toast.success(`${checkedIds.size}건 처리 완료로 표시됨 (Notion에는 반영되지 않았습니다)`, { id: t });
            reload();
        } catch (e) { toast.error('실패: ' + getErrorMessage(e), { id: t }); }
        finally { setReviewBusy(false); }
    };
    const handleBulkDismiss = async (ids) => {
        const targets = ids;
        if (targets.length === 0 || actionBusy) return;
        setReviewBusy(true);
        const t = toast.loading(`${targets.length}건 정리 중...`);
        try {
            await welfareApi.dismissPolicyChangesBulk(targets);
            toast.success(`${targets.length}건 정리됨`, { id: t });
            reload();
        } catch (e) { toast.error('실패: ' + getErrorMessage(e), { id: t }); }
        finally { setReviewBusy(false); }
    };
    const handleDismissAllLow = () => {
        const lowIds = allChanges.filter(c => c.intensity === 'low' && c.fields.some(f => !f.applied)).map(c => c.id);
        if (lowIds.length === 0) { toast.info('사소 변경 항목이 없습니다'); return; }
        if (!window.confirm(`사소 변경 ${lowIds.length}건을 검토 없이 모두 정리할까요?`)) return;
        handleBulkDismiss(lowIds);
    };

    const toggleDb = (key) => setSelected(prev => {
        const n = new Set(prev);
        n.has(key) ? n.delete(key) : n.add(key);
        return n;
    });

    // 정책 유지보수 작업은 공유 락으로 한 번에 하나만 실행된다. 각 작업의 상태 조회도
    // 같은 공통 폴러를 사용해 느린 응답이 다음 요청과 겹치지 않게 한다.
    usePolicyTaskPolling(busy && !oneClickBusy, welfareApi.getPolicyStatus, setStatus, setBusy, reload);
    usePolicyTaskPolling(syncBusy && !oneClickBusy, welfareApi.getPolicySyncStatus, setSyncStatus, setSyncBusy, reload);
    usePolicyTaskPolling(auditBusy && !oneClickBusy, welfareApi.getPolicyAuditStatus, setAuditStatus, setAuditBusy, reload);
    usePolicyTaskPolling(
        recomputeBusy && !oneClickBusy, welfareApi.getPolicyRecomputeStatus,
        setRecomputeStatus, setRecomputeBusy, reload,
    );
    usePolicyTaskPolling(
        oneClickBusy, welfareApi.getPolicyFullCheckStatus,
        setOneClickStatus, setOneClickBusy, reload,
    );
    usePolicyTaskPolling(
        restyleBusy && !oneClickBusy, welfareApi.getPolicyRestyleStatus,
        setRestyleStatus, setRestyleBusy, reload,
    );

    // 재진입 시 진행 중 작업과 아직 확인이 필요한 완료 결과를 읽기 전용으로 복원한다.
    // (다른 화면으로 이동했다 돌아오면 로컬 busy 상태가 초기화되어, 폴링이 다시 시작되지 않던 문제)
    useEffect(() => {
        (async () => {
            try {
                const results = await Promise.allSettled([
                    welfareApi.getPolicyStatus(),
                    welfareApi.getPolicySyncStatus(),
                    welfareApi.getPolicyAuditStatus(),
                    welfareApi.getPolicyRecomputeStatus(),
                    welfareApi.getPolicyFullCheckStatus(),
                    welfareApi.getPolicyRestyleStatus(),
                ]);
                setResumeError(results.some(result => result.status === 'rejected'));
                const [s, sy, au, re, oc, rs] = results.map(result => result.status === 'fulfilled' ? result.value : null);
                if (oneClickRequest.current) return;
                // Full-check owns its child work and result, including quota pauses.
                // A later standalone task takes precedence over an old full-check result.
                if (oc?.running || (policyFullCheckHasResult(oc) && ![s, sy, au, re, rs].some(item => item?.running))) {
                    setOneClickStatus(oc);
                    setOneClickBusy(Boolean(oc.running));
                    return;
                }
                if (s?.running) { setStatus(s); setBusy(true); }
                else if (policyNeedsReview(s) && !policyReviewIsMirrored(s, oc)) setStatus(s);
                if (sy?.running) { setSyncStatus(sy); setSyncBusy(true); }
                if (au?.running) { setAuditStatus(au); setAuditBusy(true); }
                else if (policyNeedsReview(au)) setAuditStatus(au);
                if (re?.running) { setRecomputeStatus(re); setRecomputeBusy(true); }
                if (oc?.running) { setOneClickStatus(oc); setOneClickBusy(true); }
                else if (policyNeedsReview(oc)) setOneClickStatus(oc);
                if (rs?.running) { setRestyleStatus(rs); setRestyleBusy(true); }
            } catch { setResumeError(true); }
        })();
    }, []);

    const handleVerify = async () => {
        const t = toast.loading('정책표 연결 확인 중...');
        try {
            const r = await welfareApi.verifyPolicyDbs();
            if (r.items?.length) {
                queryClient.setQueryData(['policyDbs'], current => ({
                    ...(current || {}),
                    dbs: r.items.map(item => {
                        const saved = displayDbs.find(db => db.key === item.key) || {};
                        return { ...saved, key: item.key, name: item.title || item.name || saved.name || item.key };
                    }),
                }));
            }
            if (r.ok) toast.success(`정책표 ${r.checked}개의 연결을 확인했습니다.`, { id: t });
            else toast.error(`연결 확인 필요: ${r.error || '보관함별 권한과 표 구성을 확인해주세요.'}`, { id: t });
        } catch (e) { toast.error('확인 실패: ' + getErrorMessage(e), { id: t }); }
    };

    // (2026-07-20) 상태 배너 정리 — 동기화/점검/재계산/스타일생성/감사/한번에점검을 한
    // 세션에서 순서대로 누르면 배너가 위에서부터 무한정 쌓이던 문제(실측) 수정.
    // 새 작업을 시작할 때 다른 배너는 지우고 지금 시작하는 것만 남긴다.
    const clearOtherStatuses = (keep) => {
        queryClient.invalidateQueries({ queryKey: ['global-policy-task-status'] });
        if (keep !== 'status') setStatus(null);
        if (keep !== 'sync') setSyncStatus(null);
        if (keep !== 'audit') setAuditStatus(null);
        if (keep !== 'recompute') setRecomputeStatus(null);
        if (keep !== 'restyle') setRestyleStatus(null);
        if (keep !== 'oneClick') setOneClickStatus(null);
    };

    const handleSync = async () => {
        if (dbKeys.length === 0) { toast.error('DB를 1개 이상 선택하세요'); return; }
        try {
            await welfareApi.syncPolicy(dbKeys);
            clearOtherStatuses('sync');
            setSyncBusy(true);
            setSyncStatus({ running: true, message: '동기화 시작…' });
            toast.info('동기화를 시작했습니다 (규모에 따라 수 분 걸릴 수 있어요)');
        } catch (e) { toast.error('동기화 시작 실패: ' + getErrorMessage(e)); }
    };

    const handleCheck = async () => {
        if (dbKeys.length === 0) { toast.error('DB를 1개 이상 선택하세요'); return; }
        try {
            await welfareApi.checkPolicy(dbKeys);
            clearOtherStatuses('status');
            setBusy(true);
            setStatus({ running: true, message: '출처 점검 시작…' });
            toast.info('점검을 시작했습니다 (원문이 바뀐 정책만 AI 분석)');
        } catch (e) { toast.error('점검 시작 실패: ' + getErrorMessage(e)); }
    };

    const handleFullCheck = async () => {
        if (dbKeys.length === 0) { toast.error('DB를 1개 이상 선택하세요'); return; }
        if (actionBusy || oneClickRequest.current) return;
        oneClickRequest.current = true;
        setOneClickPreparing(true);
        try {
            const plan = await welfareApi.getPolicyFullCheckPlan(dbKeys);
            const confirmation = policyFullCheckConfirmation(plan);
            if (confirmation && !window.confirm(confirmation)) return;
            await welfareApi.runPolicyFullCheck(dbKeys);
            clearOtherStatuses('oneClick');
            setOneClickBusy(true);
            setOneClickStatus({ running: true, phase: 'sync', message: '시작하는 중…' });
        } catch (e) { toast.error('점검 시작 실패: ' + getErrorMessage(e)); }
        finally { oneClickRequest.current = false; setOneClickPreparing(false); }
    };

    const handleAudit = async () => {
        if (dbKeys.length === 0) { toast.error('DB를 1개 이상 선택하세요'); return; }
        if (!window.confirm('아직 한 번도 비교하지 않은 정책을 Notion 현재값과 AI로 비교합니다. 실제 호출 횟수는 자료와 재시도 여부에 따라 달라집니다. 진행할까요?')) return;
        try {
            await welfareApi.auditPolicyBaseline(dbKeys);
            clearOtherStatuses('audit');
            setAuditBusy(true);
            setAuditStatus({ running: true, message: '최초 감사 시작…' });
            toast.info('최초 감사를 시작했습니다 (저장된 원문 재활용, 재요청 없음)');
        } catch (e) { toast.error('최초 감사 시작 실패: ' + getErrorMessage(e)); }
    };

    const handleRecompute = async () => {
        if (dbKeys.length === 0) { toast.error('정책표를 1개 이상 선택하세요'); return; }
        if (!window.confirm('대기 중인 진단을 저장된 데이터로 최신 기준에 맞춰 다시 계산합니다 (AI 재호출 없음, 무료). 진행할까요?')) return;
        try {
            await welfareApi.recomputePolicyDiffs(dbKeys);
            clearOtherStatuses('recompute');
            setRecomputeBusy(true);
            setRecomputeStatus({ running: true, message: '재계산 시작…' });
            toast.info('진단 재계산을 시작했습니다 (무료)');
        } catch (e) { toast.error('재계산 시작 실패: ' + getErrorMessage(e)); }
    };

    const handleRestyle = async () => {
        if (dbKeys.length === 0) { toast.error('정책표를 1개 이상 선택하세요'); return; }
        if (!window.confirm('선택한 보관함에서 문장 다듬기가 필요한 변경 제안을 작성 스타일에 맞춥니다.\n체크한 항목에만 적용되는 작업이 아닙니다. AI를 사용하며 실제 호출 횟수는 처리 결과에 따라 달라집니다.\n진행할까요?')) return;
        try {
            await welfareApi.restylePolicyChanges(dbKeys);
            clearOtherStatuses('restyle');
            setRestyleBusy(true);
            setRestyleStatus({ running: true, message: '스타일 제안 생성 시작…' });
            toast.info('스타일 제안 생성을 시작했습니다');
        } catch (e) { toast.error('시작 실패: ' + getErrorMessage(e)); }
    };

    // (2026-07-21) "검토완료로 표시"/"무시" 두 버튼이 화면상 완전히 동일하게 동작해
    // (둘 다 카드를 목록에서 치울 뿐, 상태 차이를 보여주는 화면이 없음) 혼란을 준다는
    // 지적에 따라 "처리 완료" 하나로 통합. 내부적으로는 이력 스냅샷이 남는 confirm 경로를
    // 그대로 쓴다(비용 없음, 나중에 필요해지면 되살릴 여지를 남겨둠).
    const handleConfirm = async (id, completed = false) => {
        if (actionBusy) return;
        if (!window.confirm('이 정책의 미반영 제안을 정리할까요?\nNotion 내용은 바뀌지 않습니다. 정리한 제안은 되돌릴 수 없으며, 이미 반영한 내역과 적용 취소는 유지됩니다.')) return;
        setReviewBusy(true);
        try { await welfareApi.confirmPolicyChange(id); toast.success(completed ? '완료한 카드를 정리했습니다.' : '제안을 정리했습니다. 이미 반영한 내용은 유지됩니다.'); reload(); }
        catch (e) { toast.error('실패: ' + getErrorMessage(e)); }
        finally { setReviewBusy(false); }
    };
    const handleUndoField = async (policyId, f) => {
        if (actionBusy) return;
        setApplyingPolicyIds(prev => new Set(prev).add(policyId));
        try {
            await welfareApi.undoPolicyField(policyId, f.field, f.undo_id);
            setUndoRequest(null);
            toast.success('적용 전 내용으로 되돌렸습니다. 변경 제안은 다시 검토할 수 있습니다.');
            reload();
        } catch (e) { toast.error(e?.response?.data?.detail?.message || getErrorMessage(e)); reload({preserveFields:true}); }
        finally { setApplyingPolicyIds(prev => { const next = new Set(prev); next.delete(policyId); return next; }); }
    };
    const handleApplyField = async (policyId, fieldLabel, force = false, forceSemantic = false, fieldContext = {}) => {
        if (!policyCanApply(allChanges.find(policy => policy.id === policyId))) {
            toast.warning('이 제안은 지금 반영할 수 없습니다. 정책 카드의 확인 사유를 먼저 확인해주세요.');
            return false;
        }
        try {
            const r = await welfareApi.applyPolicyField(policyId, fieldLabel, force, forceSemantic);
            // (2026-08-07, GPT Codex 지적) 백엔드가 remaining/completed를 안 돌려줄 때는
            // r.remaining이 항상 undefined라 "전체 완료"만 표시됐다 — 실제 남은 필드 수와
            // 무관하게 매번 완료로 보여 사용자가 혼란스러웠던 근본 원인. 이제 실제 값을 받는다.
            // "완료 내역으로 이동" 같은 표현은 아직 그 화면이 없으므로 쓰지 않는다(GPT 지적).
            toast.success(
                r.completed
                    ? `'${fieldLabel}' 반영 완료 — 모든 변경을 반영했습니다. 카드는 정리할 때까지 유지됩니다.`
                    : `'${fieldLabel}' 반영 완료 · 남은 변경 ${r.remaining}건`
            );
            if (!fieldContext.deferReload) reload();
            return true;
        } catch (e) {
            const code = e?.response?.data?.detail?.error;
            const msg = e?.response?.data?.detail?.message || getErrorMessage(e);
            if (policyWriteNeedsRecovery({error: code}) || !e?.response || e.response.status === 408 || e.response.status >= 500) {
                fieldContext.onUncertain?.(msg, code || 'UPDATE_UNCERTAIN');
                if (!fieldContext.deferReload) reload({preserveFields:true});
            }
            // (2026-08-07, 2차 GPT Codex 리뷰) SUSPICIOUS_VALUE는 실제 사고(id=13, 대상특성
            // 값이 세부연령에 잘못 들어가 운영 Notion에 반영됨)를 막기 위한 검증이라, 다른
            // 두 안전장치(NARROWING/BLOAT)와 같은 확인창으로 뭉뚱그리지 않고 값을 다시 보여준
            // 뒤 별도 플래그(force_semantic)로만 우회되게 한다.
            if (code === 'SUSPICIOUS_VALUE') {
                // (2026-08-07, 3차 GPT Codex 리뷰) 경고 문구·필드명만으로는 실제 기존값/적용값을
                // 안 보고도 확인창을 통과할 수 있다는 지적 — 정책명·기존값·적용값을 직접 보여준다.
                const { policyTitle, oldValue, newValue } = fieldContext;
                const proceed = window.confirm(
                    `⚠️ ${msg}\n\n` +
                    (policyTitle ? `정책: ${policyTitle}\n` : '') +
                    `필드: '${fieldLabel}'\n` +
                    `기존값: ${oldValue || '(없음)'}\n` +
                    `적용값: ${newValue || '(없음)'}\n\n` +
                    `이 값이 실제로 맞는지 원문을 다시 확인하셨나요?\n` +
                    `확실하다면 계속 진행할 수 있습니다.`
                );
                if (proceed) {
                    return await handleApplyField(policyId, fieldLabel, force, true, fieldContext);
                }
                return false;
            }
            if (code === 'NARROWING' || code === 'BLOAT') {
                if (window.confirm(`${msg}\n\n그래도 적용하시겠습니까?`)) {
                    return await handleApplyField(policyId, fieldLabel, true, forceSemantic, fieldContext);
                }
                return false;
            }
            toast.error('반영 실패: ' + msg);
            return false;
        }
    };
    const handleBulkApplyFields = async () => {
        if (!visibleFields.length || actionBusy) return;
        const items = visibleFields;
        if (!window.confirm(`정책 ${new Set(items.map(item => item.id)).size}건의 선택한 내용 ${items.length}개를 Notion에 반영할까요?\n선택하지 않은 내용은 변경하지 않습니다.`)) return;
        setBulkApplyBusy(true);
        setBulkResult(null);
        const t = toast.loading(`${items.length}건 일괄 적용 중...`);
        try {
            const reviewItems = items.filter(item => changes.find(p => p.id === item.id)?.fields.find(f => f.field === item.field)?.requires_individual_review);
            const regularItems = items.filter(item => !reviewItems.includes(item));
            const r = regularItems.length ? await welfareApi.applyPolicyFieldBulk(regularItems) : {applied: 0, skipped: [], failed: []};
            r.skipped ||= [];
            r.failed ||= [];
            let bulkInterrupted = [...r.skipped, ...r.failed].some(policyWriteNeedsRecovery);
            const holdReviewItems = pending => r.skipped.push(...pending.map(item => ({...item, reason: 'NOT_ATTEMPTED',
                message: '앞선 항목의 반영 여부를 확인하지 못해 요청하지 않았습니다. Notion의 현재 내용을 먼저 확인해주세요.'})));
            if (bulkInterrupted) {
                // This UI also sequences individually reviewed fields after
                // the bulk API. Its stop boundary must cover both groups.
                holdReviewItems(reviewItems);
            }
            for (let index = 0; !bulkInterrupted && index < reviewItems.length; index++) {
                const item = reviewItems[index];
                const policy = changes.find(p => p.id === item.id);
                const field = policy.fields.find(f => f.field === item.field);
                let applied = false;
                let uncertainMessage = null;
                let uncertainCode = 'UPDATE_UNCERTAIN';
                await applyWithSourceConfirmation(field, policy.title, message => window.confirm(message), async () => {
                    applied = await handleApplyField(item.id, item.field, false, false, {
                        policyTitle: policy.title, oldValue: field.old, newValue: field.styled || field.new,
                        deferReload: true, onUncertain: (message, code) => { uncertainMessage = message; uncertainCode = code; },
                    });
                });
                if (applied) r.applied += 1;
                else if (uncertainMessage !== null) {
                    r.failed.push({...item, error: uncertainCode, message: uncertainMessage});
                    holdReviewItems(reviewItems.slice(index + 1));
                    bulkInterrupted = true;
                }
                else r.skipped.push({...item, message: '확인을 취소했거나 적용하지 못했습니다. 내용을 다시 확인해주세요.'});
            }
            const parts = [`${r.applied}건 적용`];
            if (r.skipped?.length) parts.push(`${r.skipped.length}건 보류 — 개별 확인 필요`);
            if (r.failed?.length) parts.push(`${r.failed.length}건 실패`);
            if (bulkInterrupted) parts.push('추가 반영 중단 — Notion 확인 필요');
            const issues = [...(r.skipped || []), ...(r.failed || [])].map(item => ({
                ...item, title: allChanges.find(policy => policy.id === item.id)?.title || `정책 ${item.id}`,
            }));
            setBulkResult({ summary: parts.join(' · '), issues });
            (issues.length ? toast.warning : toast.success)(parts.join(' · '), { id: t });
            if (bulkInterrupted) {
                const requestedKeys = new Set(items.map(item => `${item.id}::${item.field}`));
                const unresolvedKeys = new Set(issues.map(item => `${item.id}::${item.field}`));
                setCheckedFields(previous => new Set([...previous].filter(key =>
                    !requestedKeys.has(key) || unresolvedKeys.has(key))));
                reload({ preserveFields: true });
            } else reload();
        } catch (e) {
            toast.error('일괄 적용 실패: ' + getErrorMessage(e), { id: t });
        } finally {
            setBulkApplyBusy(false);
        }
    };

    const handleDraft = async (item) => {
        if (dismissingCandidateRef.current.has(item.id)) return;
        const dbKey = draftPick[item.id] || item.similar?.db_key || displayDbs[0]?.key;
        if (!dbKey) return toast.info('먼저 설정에서 정책표를 연결해주세요.');
        if (urlDraftNeedsCheck && urlDraftStatus.url === item.link) return;
        const operation = beginUrlDraftOperation('create');
        if (!operation) return;
        const t = toast.loading('Notion 초안 생성 중...');
        try {
            const r = await welfareApi.draftPolicy(item.id, dbKey);
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            toast.success('초안이 추가되었습니다', { id: t });
            reload();
        } catch (e) {
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            if (item.link) {
                urlDraftGeneration.current++;
                setUrlDraftInput({url:item.link,title:item.title || '',dbKey:dbKey || ''});
                clearUrlDraftPreview();
                setUrlDraftStatus({state:'unavailable',url:item.link,
                    message:'이 후보의 등록 여부를 확인하지 못했습니다. 다시 추가하기 전에 Notion 등록 상태를 확인해주세요.'});
                setUrlDraftOpen(true);
                requestAnimationFrame(()=>document.getElementById('policy-url-draft-panel')?.scrollIntoView({block:'start',behavior:'auto'}));
            }
            toast.error('초안 등록 확인 필요: ' + getErrorMessage(e), { id: t });
        } finally {finishUrlDraftOperation(operation);}
    };
    const updateUrlDraftField = (key, value) => {
        if (urlDraftOperation.current) return;
        resetUrlDraftSourceReview();
        setUrlDraftPreview(previous => previous ? {
            ...previous,
            fields: { ...previous.fields, [key]: value },
        } : previous);
    };
    const toggleUrlDraftOption = (key, value) => {
        const current = urlDraftPreview?.fields?.[key] || [];
        updateUrlDraftField(key, current.includes(value)
            ? current.filter(item => item !== value)
            : [...current, value]);
    };
    const handleUrlDraftPreview = async (event) => {
        event?.preventDefault();
        const url = urlDraftInput.url.trim();
        if (!url) return toast.info('정책이 있는 웹주소를 입력해주세요.');
        if (!urlDraftInput.dbKey) return toast.info('저장할 정책표를 선택해주세요.');
        if (urlDraftIsDocument && !urlDraftPrivacyReviewed) return toast.info('원문 파일을 직접 확인한 뒤 이 파일의 외부 AI 전송에 동의해주세요.');
        const operation = beginUrlDraftOperation('preview');
        if (!operation) return;
        clearUrlDraftPreview();
        setUrlDraftPreviewFailed(false);
        const privacyReviewed = urlDraftIsDocument && urlDraftPrivacyReviewed;
        setUrlDraftPrivacyReviewed(false); // One request only; retries require a fresh confirmation.
        const t = toast.loading('주소의 정책 내용을 읽고 있어요...');
        try {
            const result = await welfareApi.previewPolicyDraftFromUrl(
                url, urlDraftInput.dbKey, urlDraftInput.title.trim(), privacyReviewed);
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            urlDraftPreviewGeneration.current = operation.generation;
            resetUrlDraftSourceReview();
            setUrlDraftPreview(result);
            toast.success('초안을 만들었습니다. 내용을 확인해주세요.', { id: t });
        } catch (e) {
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            setUrlDraftPreviewFailed(true);
            if (e.response?.data?.detail?.error === 'AI_DOCUMENT_REVIEW_REQUIRED') setUrlDraftDocumentDetected(true);
            const needsReview = e?.response?.data?.detail?.error === 'POLICY_DRAFT_REVIEW_REQUIRED';
            const reviewMessage = e?.response?.data?.detail?.message;
            toast.error(needsReview && typeof reviewMessage === 'string'
                ? '원문 확인 필요: ' + reviewMessage
                : '초안 분석 실패: ' + getErrorMessage(e), { id: t });
        } finally {
            finishUrlDraftOperation(operation);
        }
    };
    const handleUrlDraftCreate = async () => {
        if (urlDraftNeedsCheck || urlDraftPreviewGeneration.current !== urlDraftGeneration.current) return;
        if (!draftSourceConfirmed(urlDraftSourceChecksRef.current)) {
            return toast.info('원문과 초안을 비교한 뒤 확인란에 체크해주세요.');
        }
        if (!urlDraftPreview?.fields?.title?.trim()) {
            return toast.info('사업명을 입력해주세요.');
        }
        const ageError=draftAgeRangeError(urlDraftPreview.fields);
        if(ageError)return toast.info(ageError);
        const operation = beginUrlDraftOperation('create');
        if (!operation) return;
        const t = toast.loading('확인한 내용을 Notion에 추가하고 있어요...');
        try {
            const result = await welfareApi.createPolicyDraftFromUrl(
                urlDraftPreview.url, urlDraftPreview.db_key, urlDraftPreview.fields, true);
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            if (result.warning) toast.warning(result.warning, { id: t });
            else toast.success('Notion에 정책 초안을 추가했습니다.', { id: t });
            setUrlDraftCreated({title: urlDraftPreview.fields.title, db_key: urlDraftPreview.db_key,
                warning: result.warning, documentSource: urlDraftPreview.analyzed_from === 'document' || urlDraftIsDocument});
            clearUrlDraftPreview();
            setUrlDraftStatus(null);
            setUrlDraftInput(previous => ({ ...previous, url: '', title: '' }));
            reload();
        } catch (e) {
            if (!currentUrlDraftOperation(operation)) {toast.dismiss(t);return;}
            setUrlDraftStatus({state:'unavailable',url:urlDraftInput.url.trim(),
                message:'등록 여부를 확인하지 못했습니다. 다시 추가하기 전에 Notion 등록 상태를 확인해주세요.'});
            toast.error('Notion 초안 등록 확인 필요: ' + getErrorMessage(e), { id: t });
        } finally {
            finishUrlDraftOperation(operation);
        }
    };
    const handleUrlDraftStatus = async () => {
        const url = urlDraftInput.url.trim();
        if (!url) return toast.info('확인할 정책 안내 페이지 주소를 입력해주세요.');
        const operation = beginUrlDraftOperation('status');
        if (!operation) return;
        setUrlDraftStatus(null);
        try {
            const result = await welfareApi.checkPolicyDraftStatus(url);
            if (!currentUrlDraftOperation(operation)) return;
            setUrlDraftStatus({...result, url});
            if (result.state === 'completed') reload();
        } catch (e) {
            if (!currentUrlDraftOperation(operation)) return;
            setUrlDraftStatus({state:'unavailable',url,message:'등록 상태를 확인하지 못했습니다. 잠시 후 다시 확인해주세요.'});
            toast.error('등록 상태 확인 필요: ' + getErrorMessage(e));
        } finally { finishUrlDraftOperation(operation); }
    };
    const handleUrlDraftRetry = async () => {
        if (urlDraftOperation.current || !urlDraftStatus?.can_retry || !urlDraftStatus.attempt_id || urlDraftStatus.url !== urlDraftInput.url.trim()) return;
        if (!window.confirm(`원래 Notion 정책표에서 아래 출처의 정책이 등록되지 않았음을 직접 확인하셨나요?\n${urlDraftStatus.url}\n이미 등록됐다면 재시도 시 중복될 수 있습니다. 확인하지 않았다면 취소해주세요.`)) return;
        const operation = beginUrlDraftOperation('retry');
        if (!operation) return;
        try {
            const result = await welfareApi.allowPolicyDraftRetry(urlDraftStatus.url, urlDraftStatus.attempt_id);
            if (!currentUrlDraftOperation(operation)) return;
            setUrlDraftStatus(null);
            toast.info(result.message);
        } catch (e) { if (currentUrlDraftOperation(operation)) toast.error(getErrorMessage(e)); }
        finally { finishUrlDraftOperation(operation); }
    };
    const handleDismissCandidate = async (id) => {
        if (urlDraftOperation.current || dismissingCandidateRef.current.has(id)) return;
        if (!window.confirm('이 후보를 다시 표시하지 않을까요?')) return;
        // Lock synchronously as well as in the UI: rapid clicks must not enqueue duplicates.
        dismissingCandidateRef.current.add(id);
        setDismissingCandidateIds(new Set(dismissingCandidateRef.current));
        try {
            await welfareApi.dismissNotionCandidates([id]);
            if (urlDraftMounted.current) toast.success('제외됨');
            // Excluding a candidate does not change policy review fields. A late completion
            // must not erase selections made after navigating back to policy review.
            await queryClient.invalidateQueries({ queryKey: ['policyCandidates'] });
        } catch (e) {
            if (urlDraftMounted.current) toast.error('실패: ' + getErrorMessage(e));
        } finally {
            dismissingCandidateRef.current.delete(id);
            if (urlDraftMounted.current) setDismissingCandidateIds(new Set(dismissingCandidateRef.current));
        }
    };

    // (버그수정) window.open()은 Tauri 웹뷰에서 무시된다 — App.jsx의 전역 클릭 핸들러는
    // <a target="_blank">만 감지하는데 이 버튼들은 <button>이라 잡히지 않았음. opener
    // 플러그인을 직접 호출해야 실제 시스템 브라우저가 열린다.
    const openLink = async (url) => {
        if (!url) return;
        try {
            const { invoke } = await import('@tauri-apps/api/core');
            await invoke('plugin:opener|open_url', { url });
        } catch {
            try { window.open(url, '_blank'); } catch { /* noop */ }
        }
    };

    const urlDraftMissing = urlDraftPreview ? Object.entries(URL_DRAFT_REQUIRED_LABELS)
        .filter(([key]) => {
            const value = urlDraftPreview.fields?.[key];
            return value === null || value === undefined || value === ''
                || (Array.isArray(value) && value.length === 0);
        })
        .map(([, label]) => label) : [];

    return (
        <div className="space-y-6 max-w-5xl mx-auto pb-8">
            {undoRequest && <PolicyUndoDialog request={undoRequest} busy={actionBusy} onClose={() => setUndoRequest(null)} onConfirm={() => handleUndoField(undoRequest.id, undoRequest.field)} />}
            <div className="flex justify-between items-start">
                <div>
                    <h1 className="ui-page-title">
                        정책 최신화
                    </h1>
                    {pageTab === 'main' && <p className="text-sm text-gray-500 mt-2">
                        연결한 정책표와 <b>출처 페이지</b>를 비교해 <b>변경 제안</b>을 준비합니다.
                        확인한 내용만 선택해 Notion에 반영하세요.
                    </p>}
                </div>
            </div>

            <div role="tablist" aria-label="정책 작업" className="flex flex-wrap gap-1 border-b border-gray-200 dark:border-gray-700">
                {[
                    ['main', `정책 최신화 (${displayDbs.length}개 보관함)`],
                    ['watch', '웹페이지 변경 확인'],
                    ['candidates', `정책 등록 후보${candsQ.isError ? ' · 조회 실패' : candsQ.isLoading ? ' · 확인 중' : ` (${candidates.length})`}`],
                ].map(([key, label]) => (
                    <button key={key} id={`policy-tab-${key}`} type="button" role="tab"
                        aria-selected={pageTab === key} aria-controls={`policy-panel-${key}`} tabIndex={pageTab === key ? 0 : -1}
                        disabled={selectionBusy || urlDraftLocked}
                        onClick={() => setPageTab(key)}
                        onKeyDown={event => {
                            const keys = ['main', 'watch', 'candidates'];
                            const index = keys.indexOf(key);
                            const next = event.key === 'ArrowRight' ? keys[(index + 1) % keys.length]
                                : event.key === 'ArrowLeft' ? keys[(index + keys.length - 1) % keys.length]
                                    : event.key === 'Home' ? keys[0] : event.key === 'End' ? keys.at(-1) : null;
                            if (next) {
                                event.preventDefault();
                                setPageTab(next);
                                document.getElementById(`policy-tab-${next}`)?.focus();
                            }
                        }}
                        className={`min-h-11 px-3 py-2 font-medium text-sm transition-colors border-b-2 flex items-center gap-1.5 disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 ${pageTab === key
                            ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                            : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'}`}>
                        {label}
                        {key === 'main' && visibleFields.length > 0 && <span className="text-xs">· 선택 {visibleFields.length}개</span>}
                    </button>
                ))}
            </div>

            {pageTab === 'watch' && <div id="policy-panel-watch" role="tabpanel" aria-labelledby="policy-tab-watch"><PolicyWatch embedded /></div>}

            {pageTab === 'main' && (
            <section id="policy-panel-main" role="tabpanel" aria-labelledby="policy-tab-main" className="space-y-6">
            {/* 컨트롤: 연결된 보관함 선택 + 한 번에 점검 (B1) */}
            <div className="bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 p-4 space-y-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex min-w-0 flex-1 flex-wrap gap-2">
                    {displayDbs.map(d => {
                        const on = selected.has(d.key);
                        return (
                            <button key={d.key} disabled={selectionBusy} aria-pressed={selected.has(d.key)} onClick={() => toggleDb(d.key)}
                                title={`로컬 ${d.local_count || 0}건 · 마지막 동기화 ${formatStoredUtcTime(d.last_sync_at)}${d.last_error ? ` · 확인 필요: ${d.last_error}` : ''}`}
                                className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm border transition-colors ${on
                                    ? 'bg-indigo-100 border-indigo-300 text-indigo-900 dark:bg-indigo-950 dark:border-indigo-700 dark:text-indigo-100'
                                    : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                                {d.name}
                            </button>
                        );
                    })}
                    {dbsQ.isLoading && <p role="status">정책표를 불러오는 중입니다…</p>}
                    {dbsQ.isSuccess && !displayDbs.length && <div className="w-full rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">정책 최신화를 처음 사용하시나요? <Link to="/settings?tab=guide&step=policy" className="font-semibold underline underline-offset-2">정책 최신화 준비하기</Link>에서 Notion 연결과 정책표 설정을 순서대로 완료하세요. 새 소식 보관함은 필요하지 않습니다.</div>}
                </div>
                {displayDbs.length > 0 && <button type="button" onClick={handleVerify}
                    className="flex min-h-9 shrink-0 items-center gap-1 rounded-lg border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> 연결 확인
                </button>}
                </div>
                <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-4 dark:border-gray-700">
                    <Tooltip content="최신 내용 불러오기부터 최초·변경 비교, 필요한 제안 점검과 문장 다듬기까지 진행합니다" wrapperClassName="w-full sm:w-1/2">
                        <button onClick={handleFullCheck} aria-describedby="policy-ai-usage" disabled={!dbKeys.length || actionBusy || aiStatus?.busy}
                            className="min-h-14 w-full justify-center px-6 py-4 text-base font-semibold bg-indigo-600 text-white rounded-xl hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2 shadow-md focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:ring-offset-2">
                            {oneClickBusy || oneClickPreparing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCcwDot className="w-4 h-4" />}
                            {oneClickPreparing ? '점검 대상 확인 중…' : '정책 정보 최신인지 확인하기'}
                        </button>
                    </Tooltip>
                    <button onClick={() => setAdvancedOpen(o => !o)}
                        aria-expanded={advancedOpen} aria-controls="policy-detail-actions" className="min-h-11 rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 dark:text-gray-200 flex items-center gap-2 ml-auto">
                        세부 작업 {advancedOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                    </button>
                </div>

                <p id="policy-ai-usage" className="text-xs leading-5 text-gray-500 dark:text-gray-400">최초·변경 비교와 필요한 문장 정리·검색 데이터 생성에 AI를 사용합니다. Notion에는 자동으로 반영하지 않습니다.</p>

                {showFirstPolicyHelp && <div role="note" aria-label="첫 정책 시작 안내" className="rounded-xl border border-indigo-100 bg-indigo-50/40 px-4 py-3 text-sm leading-6 dark:border-indigo-900 dark:bg-indigo-950/20">
                    <p>정책표가 비어 있다면 정책 상세 안내 주소로 첫 초안을 만들고, 원문을 확인한 뒤 Notion에 추가하세요.</p>
                    <button type="button" onClick={openFirstPolicyDraft} disabled={actionBusy} className="min-h-10 font-semibold text-indigo-700 underline underline-offset-2 disabled:opacity-50 dark:text-indigo-300">첫 정책 초안 만들기</button>
                    <p className="text-xs text-gray-500 dark:text-gray-400">이미 Notion에 정책을 입력했다면 위의 ‘정책 정보 최신인지 확인하기’를 누르세요.</p>
                </div>}

                {advancedOpen && (
                    <div id="policy-detail-actions" role="region" aria-label="세부 작업" className="grid grid-cols-1 gap-3 pt-4 [&_button]:min-h-11 border-t border-gray-100 dark:border-gray-700 sm:grid-cols-2">
                        <h3 className="sm:col-span-2 text-sm font-semibold text-gray-700 dark:text-gray-200">정책 확인·불러오기</h3>
                        <p className="sm:col-span-2 text-xs leading-5 text-gray-500 dark:text-gray-400">위의 전체 점검에 포함된 단계입니다. 특정 단계만 다시 실행하거나 확인할 때 사용하세요.</p>

                        <div className="min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                            <button onClick={handleSync} disabled={!dbKeys.length || actionBusy || aiStatus?.busy}
                                className="min-h-11 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 flex flex-wrap items-center gap-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800">
                                {syncBusy && <Loader2 className="w-4 h-4 animate-spin" />}
                                Notion 정책 불러오기
                                <span className="text-xs px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">필요 시 AI 사용</span>
                            </button>
                            <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">Notion에서 수정한 내용을 가져옵니다. 새 정책·제목이 바뀐 정책·검색용 데이터가 없는 정책은 제목을 Gemini로 보내 검색용 데이터를 만들 수 있습니다. 정책 내용 비교는 별도 작업입니다.</p>
                        </div>
                        <div className="min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                            <button onClick={handleCheck} disabled={!dbKeys.length || actionBusy || aiStatus?.busy}
                                className="min-h-11 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 flex flex-wrap items-center gap-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800">
                                {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                                출처 변경 확인
                                <span className="text-xs px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">변경 시 AI 사용</span>
                            </button>
                            <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">바뀐 원문이 있는지 점검하고, 변경된 정책만 AI로 분석합니다.</p>
                        </div>
                        <div className="min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                            <button onClick={handleAudit} disabled={!dbKeys.length || actionBusy || aiStatus?.busy}
                                className="min-h-11 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 flex flex-wrap items-center gap-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800">
                                {auditBusy && <Loader2 className="w-4 h-4 animate-spin" />}
                                아직 비교하지 않은 정책 점검
                                <span className="text-xs px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">AI 사용</span>
                            </button>
                            <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">아직 분석하지 않은 정책을 비교합니다.</p>
                        </div>
                        <div className="min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                            <button onClick={handleRecompute} disabled={!dbKeys.length || actionBusy}
                                className="min-h-11 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 flex flex-wrap items-center gap-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800">
                                {recomputeBusy && <Loader2 className="w-4 h-4 animate-spin" />}
                                저장된 변경 제안 다시 점검
                                <span className="text-xs px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-300">AI 사용 없음</span>
                            </button>
                            <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">
                                원문 재수집 없이 저장된 제안을 재검토합니다.
                            </p>
                        </div>
                        <h3 className="sm:col-span-2 text-sm font-semibold text-gray-700 dark:text-gray-200">문장 다듬기</h3>
                <div className="sm:col-span-2 min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                    <Link to="/settings?tab=keywords&section=style" className="mb-3 inline-flex min-h-11 items-center rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200">정책 작성 스타일 설정</Link>
                    <button type="button" onClick={handleRestyle} disabled={!allChanges.length || !dbKeys.length || actionBusy || aiStatus?.busy || changesQ.isError || changesQ.isLoading} aria-describedby="policy-restyle-scope" className="min-h-11 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 flex flex-wrap items-center gap-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800">제안 문장 다듬기 <span className="text-xs px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">AI 사용</span></button>
                    <p id="policy-restyle-scope" className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">선택한 보관함의 변경 제안을 작성 스타일에 맞춰 다듬습니다.</p>
                </div>
                    </div>
                )}
            </div>

            {/* 한 번에 점검 진행 */}
            {oneClickStatus && (
                <div data-policy-full-check role="status" aria-live="polite" className="bg-indigo-50/60 dark:bg-indigo-950/10 border border-indigo-100 dark:border-indigo-900/30 rounded-lg p-3 text-sm text-indigo-700 dark:text-indigo-300 flex items-center gap-2">
                    {oneClickStatus.running ? <Loader2 className="w-4 h-4 animate-spin shrink-0" /> : policyNeedsReview(oneClickStatus) || policyFullCheckNeedsAttention(oneClickStatus) ? <AlertTriangle className="w-4 h-4 shrink-0 text-amber-600" aria-label={policyReviewNotes(oneClickStatus).length ? '확인이 필요한 정책 있음' : '점검 작업 확인 필요'} /> : <CheckCircle2 className="w-4 h-4 shrink-0" />}
                    <div className="flex-1 min-w-0 break-words">
                    <p className="font-medium">{policyFullCheckPhase(oneClickStatus)}
                        {oneClickStatus.running && Number.isFinite(oneClickStatus.total) && oneClickStatus.total > 0 && <span className="ml-2 font-normal">{Math.max(0, Math.min(oneClickStatus.total, Number(oneClickStatus.checked) || 0))}/{oneClickStatus.total}건</span>}
                    </p>
                    <p className="mt-1">{oneClickStatus.message || '대기'}</p>
                    {policyFullCheckNeedsAttention(oneClickStatus) && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">완료된 작업은 저장되어 있습니다. 안내된 사유를 확인하고, 한도에 도달했다면 한도가 회복된 뒤 ‘정책 정보 최신인지 확인하기’로 남은 작업을 이어서 진행하세요.</p>}
                    {(oneClickStatus.ai_required > 0 || oneClickStatus.ai_failed > 0 || oneClickStatus.ai_preflight_held > 0) && (
                        <span className="block mt-1 text-xs text-indigo-500 dark:text-indigo-400">
                            AI 분석 대상 {oneClickStatus.ai_required || 0} · 분석 완료 {oneClickStatus.ai_completed || 0}
                            {' · '}호출 간격 대기 {oneClickStatus.ai_rate_waited || 0} · 분석 실패 {oneClickStatus.ai_failed || 0}
                            {' · '}AI 호출 전 보류 {oneClickStatus.ai_preflight_held || 0}
                            {' · '}실제 내용 변경 {oneClickStatus.changed || 0}
                        </span>
                    )}
                    {(oneClickStatus.style_failed > 0 || oneClickStatus.style_rejected_fields > 0) && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">문장 정리 완료 {oneClickStatus.styled || 0}건 · 실패 {oneClickStatus.style_failed || 0}건 · 검증 보류 {oneClickStatus.style_rejected_fields || 0}개 항목</p>}
                    {(oneClickStatus.embedding_completed > 0 || oneClickStatus.embedding_failed > 0) && <p className="mt-1 text-xs text-indigo-500 dark:text-indigo-400">제목 검색 데이터 준비 완료 {oneClickStatus.embedding_completed || 0}건 · 실패 {oneClickStatus.embedding_failed || 0}건{oneClickStatus.embedding_failed > 0 && ' · 이미 완료한 정책 비교와 제안은 유지됩니다.'}</p>}
                    <PolicyReviewDetails status={oneClickStatus} /></div>
                    {!oneClickStatus.running && (
                        <button onClick={() => setOneClickStatus(null)} aria-label="통합 점검 안내 닫기" className="w-11 h-11 shrink-0 self-start flex items-center justify-center rounded-lg text-indigo-500 hover:bg-indigo-100 dark:text-indigo-300 dark:hover:bg-indigo-900"><X size={16} /></button>
                    )}
                </div>
            )}

            {/* 진단 재계산 진행 */}
            {recomputeStatus && !oneClickStatus && (
                <div className="bg-gray-50 dark:bg-gray-800/40 border border-gray-200 dark:border-gray-700 rounded-lg p-3 text-sm text-gray-600 dark:text-gray-300 flex items-center gap-2">
                    {recomputeStatus.running ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                    <span className="flex-1">{recomputeStatus.message || '대기'}
                    {recomputeStatus.running && recomputeStatus.total > 0 && (
                        <span className="text-gray-400"> ({recomputeStatus.checked}/{recomputeStatus.total})</span>
                    )}</span>
                    {!recomputeStatus.running && (
                        <button onClick={() => setRecomputeStatus(null)} className="text-gray-400 hover:text-gray-600"><X size={14} /></button>
                    )}
                </div>
            )}

            {/* 동기화 진행 — 통합점검이 도는 동안엔 숨김: 통합 배너가 같은 메시지를 미러링해
                보여주고 있어서, 여기까지 그리면 같은 내용의 배너가 2개 뜬다(재진입 시 실측) */}
            {syncStatus && !oneClickStatus && (
                <div className="bg-indigo-50/60 dark:bg-indigo-950/10 border border-indigo-100 dark:border-indigo-900/30 rounded-lg p-3 text-sm text-indigo-700 dark:text-indigo-300 flex items-center gap-2">
                    {syncStatus.running ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                    <span className="flex-1">{syncStatus.message || '대기'}
                    {syncStatus.running && syncStatus.total > 0 && (
                        <span className="text-indigo-400"> ({syncStatus.checked}/{syncStatus.total})</span>
                    )}</span>
                    {!syncStatus.running && (
                        <button onClick={() => setSyncStatus(null)} className="text-indigo-400 hover:text-indigo-600"><X size={14} /></button>
                    )}
                </div>
            )}

            {/* 최초 감사 진행 */}
            {auditStatus && !oneClickStatus && (
                <div className="bg-amber-50/60 dark:bg-amber-950/10 border border-amber-100 dark:border-amber-900/30 rounded-lg p-3 text-sm text-amber-700 dark:text-amber-300 flex items-center gap-2">
                    {auditStatus.running ? <Loader2 className="w-4 h-4 animate-spin" /> : policyNeedsReview(auditStatus) ? <AlertTriangle className="w-4 h-4 shrink-0 text-amber-600" aria-label="확인이 필요한 정책 있음" /> : <CheckCircle2 className="w-4 h-4" />}
                    <div className="flex-1 min-w-0">{auditStatus.message || '대기'}
                    {auditStatus.running && auditStatus.total > 0 && (
                        <span className="text-amber-400"> ({auditStatus.checked}/{auditStatus.total})</span>
                    )}
                    <PolicyReviewDetails status={auditStatus} /></div>
                    {!auditStatus.running && (
                        <button onClick={() => setAuditStatus(null)} aria-label="최초 비교 안내 닫기" className="w-11 h-11 shrink-0 self-start flex items-center justify-center rounded-lg text-amber-700 hover:bg-amber-100 dark:text-amber-300 dark:hover:bg-amber-900"><X size={16} /></button>
                    )}
                </div>
            )}

            {/* (T3) 스타일 제안 생성 진행 */}
            {restyleStatus && !oneClickStatus && (
                <div className="bg-purple-50/60 dark:bg-purple-950/10 border border-purple-100 dark:border-purple-900/30 rounded-lg p-3 text-sm text-purple-700 dark:text-purple-300 flex items-center gap-2">
                    {restyleStatus.running ? <Loader2 className="w-4 h-4 animate-spin" /> : restyleNeedsReview(restyleStatus) ? <AlertTriangle className="w-4 h-4 shrink-0 text-amber-600" aria-label="검토가 필요한 항목 있음" /> : <CheckCircle2 className="w-4 h-4" />}
                    <span className="flex-1">{restyleStatus.message || '대기'}
                    {restyleNeedsReview(restyleStatus) && <span className="block mt-1 text-xs text-amber-700 dark:text-amber-300">보류된 결과는 저장하지 않았습니다. 기존 제안과 원문을 대조해주세요. 문장 다듬기는 내용의 정확성을 보장하지 않습니다.</span>}
                    {restyleStatus.running && restyleStatus.total > 0 && (
                        <span className="text-purple-400"> ({restyleStatus.checked}/{restyleStatus.total})</span>
                    )}</span>
                    {!restyleStatus.running && (
                        <button onClick={() => setRestyleStatus(null)} className="text-purple-400 hover:text-purple-600"><X size={14} /></button>
                    )}
                </div>
            )}

            {/* 점검 요약 — 통합점검 중엔 통합 배너가 미러링하므로 숨김(중복 배너 방지) */}
            {status && !oneClickStatus && (
                <div className="bg-indigo-50/60 dark:bg-indigo-950/10 border border-indigo-100 dark:border-indigo-900/30 rounded-lg p-3 text-sm text-indigo-700 dark:text-indigo-300 flex items-center gap-2">
                    {status.running ? <Loader2 className="w-4 h-4 animate-spin" /> : policyNeedsReview(status) ? <AlertTriangle className="w-4 h-4 shrink-0 text-amber-600" aria-label="확인이 필요한 정책 있음" /> : <CheckCircle2 className="w-4 h-4" />}
                    <div className="flex-1 min-w-0">{status.message || '대기'}
                    {status.running && status.total > 0 && (
                        <span className="text-indigo-400"> ({status.checked}/{status.total})</span>
                    )}
                    {(status.ai_required > 0 || status.ai_failed > 0 || status.ai_preflight_held > 0) && (
                        <span className="block mt-1 text-xs text-indigo-500 dark:text-indigo-400">
                            AI 분석 대상 {status.ai_required || 0} · 분석 완료 {status.ai_completed || 0}
                            {' · '}호출 간격 대기 {status.ai_rate_waited || 0} · 분석 실패 {status.ai_failed || 0}
                            {' · '}AI 호출 전 보류 {status.ai_preflight_held || 0}
                            {' · '}실제 내용 변경 {status.changed || 0}
                        </span>
                    )}
                    <PolicyReviewDetails status={status} /></div>
                    {!status.running && (
                        <button onClick={() => setStatus(null)} aria-label="정책 점검 안내 닫기" className="w-11 h-11 shrink-0 self-start flex items-center justify-center rounded-lg text-indigo-500 hover:bg-indigo-100 dark:text-indigo-300 dark:hover:bg-indigo-900"><X size={16} /></button>
                    )}
                </div>
            )}

            {/* (S3) 상시 상태 배너 — 변경감지 여부와 무관하게 항상 노출. 각 목록은 토글로 펼침 */}
            {(deadLinkPolicies.length > 0 || noSourcePolicies.length > 0) && (
                <div className="bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-900/40 rounded-lg p-3 text-sm text-amber-700 dark:text-amber-300 space-y-2">
                    <HealthToggleList icon={<Link2Off size={15} />} label="깨진 출처 링크"
                        items={deadLinkPolicies} openLink={openLink} />
                    <HealthToggleList icon={<AlertTriangle size={15} />} label="출처 주소 없음"
                        items={noSourcePolicies} openLink={openLink} />
                </div>
            )}

            <PolicyReviewQueue key={JSON.stringify([...dbKeys].sort())} dbKeys={dbKeys} excludePolicyIds={inlineReviewIds} />

            {/* 변경 감지 섹션 */}
            {resumeError && <p role="alert" className="rounded-xl bg-amber-50 p-3 text-sm text-amber-800">일부 작업의 진행 상태를 확인하지 못했습니다. 연결 상태를 확인한 뒤 다른 화면에 갔다가 다시 열어주세요.</p>}
            {[status, syncStatus, auditStatus, recomputeStatus, restyleStatus, oneClickStatus].some(item => item?.connectionError) && (
                <p role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">진행 상태를 확인하지 못했습니다. 연결을 다시 확인하고 있습니다. 작업이 중단됐다는 뜻은 아닙니다.</p>
            )}
            {dbsQ.isError && <p role="alert" className="text-red-700">정책표 목록을 불러오지 못했습니다. <button onClick={() => dbsQ.refetch()} className="underline">다시 불러오기</button></p>}
            {healthQ.isError && <p role="alert" className="text-amber-800">출처 연결 상태를 확인하지 못했습니다. <button onClick={() => healthQ.refetch()} className="underline">다시 확인</button></p>}
            <section>
                <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                    <div className="flex flex-wrap items-center gap-3">
                        <h2 className="text-xl font-semibold text-gray-800 dark:text-gray-100">정책 내용 변경 감지 <span className="text-sm font-normal text-gray-400">(저장된 결과 {changes.length}/{allChanges.length}건 표시)</span></h2>
                        <button type="button" disabled={!visibleFields.length || actionBusy || changesQ.isError} onClick={handleBulkApplyFields} className="rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50">선택한 내용 Notion에 반영 ({visibleFields.length})</button>
                    </div>
                    {/* (S1+B3) 강도 필터 칩 — 기본은 중요·보강만, 사소는 접어서 별도 노출 */}
                    <div className="flex gap-1.5 items-center flex-wrap">
                        {[['important', '주요·보강', null], ['high', '주요', 'text-amber-600'], ['info', '보강', 'text-sky-600']].map(([key, label, dotColor]) => (
                            <button key={key} disabled={selectionBusy} aria-pressed={intensityFilter === key} onClick={() => setIntensityFilter(key)}
                                className={`flex items-center gap-1 px-2.5 py-1 rounded-full text-xs border transition-colors ${intensityFilter === key
                                    ? 'bg-indigo-600 text-white border-indigo-600'
                                    : 'border-gray-300 dark:border-gray-600 text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                                {dotColor && <Circle className={`w-2 h-2 ${dotColor}`} fill="currentColor" strokeWidth={0} />}
                                {label}
                            </button>
                        ))}
                        {lowCount > 0 && (
                            <button
                                disabled={selectionBusy}
                                onClick={() => setIntensityFilter(intensityFilter === 'all' ? 'important' : 'all')}
                                className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs border border-dashed border-gray-300 dark:border-gray-600 text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800">
                                {intensityFilter !== 'all' && <Circle className="w-2 h-2 text-gray-300 dark:text-gray-600" fill="currentColor" strokeWidth={0} />}
                                {intensityFilter === 'all' ? '사소한 변경 접기' : `사소한 변경 ${lowCount}건 보기`}
                            </button>
                        )}
                    </div>
                </div>

                {changesQ.isError ? (
                    <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">변경 목록을 불러오지 못했습니다. 변경이 없다는 뜻은 아닙니다. {changesQ.error?.code === 'ECONNABORTED' ? '응답 시간이 초과되었습니다.' : changesQ.error?.response?.status >= 500 ? '앱 내부 서버에서 목록을 처리하지 못했습니다.' : '목록 조회 연결을 확인해주세요.'} <button onClick={() => changesQ.refetch()} className="underline font-semibold">다시 불러오기</button></div>
                ) : changesQ.isLoading ? (
                    <div className="p-4 text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 로딩 중...</div>
                ) : changes.length === 0 ? (
                    <div className="p-6 text-center text-gray-400 bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 text-sm">
                        {!dbKeys.length ? '점검할 정책표를 선택해주세요.' : allChanges.length === 0 ? '표시할 변경 제안이 없습니다. 최신 상태를 확인하려면 위에서 정책 점검을 실행하세요.' : '이 필터에 해당하는 항목이 없습니다.'}
                    </div>
                ) : (
                    <div className="space-y-3">
                        {changes.some(c=>!policyCanApply(c)&&!policyRecoveryRequired(c)&&c.review_state!=='completed')&&<p className="text-xs leading-relaxed text-gray-500 dark:text-gray-400">재점검이 필요한 제안은 위의 ‘정책 정보 최신인지 확인하기’로 다시 점검한 뒤 반영할 수 있습니다. 저장된 제안은 유지됩니다.</p>}
                        {changes.slice(0, displayLimit).map(c => {
                          const im = INTENSITY_META[c.intensity] || INTENSITY_META.low;
                          const fieldGroups = groupPolicyFields(c.fields);
                          // Receipts survive proposal replacement and may concern the same field.
                          const history = Array.isArray(c.apply_history) ? c.apply_history : [];
                          fieldGroups.applied = [...fieldGroups.applied.filter(f => !history.some(h => h.undo_id === f.undo_id)), ...history];
                          const historyNeedsVerification = fieldGroups.applied.some(f => f.needs_verification);
                          const applyReady = policyCanApply(c);
                          const reviewLabel = policyStateLabel(c);
                          const reviewNotes = policyCardReviewNotes(c);
                          const completed = fieldGroups.proposalCount === 0 && fieldGroups.applied.length > 0;
                          const proposedFields = [...fieldGroups.priority, ...fieldGroups.minor];
                          const selectedCount = fields => fields.filter(f => checkedFields.has(`${c.id}::${f.field}`)).length;
                          const renderField = f => (
                              <FieldDiff key={f.undo_id || f.field} f={f} applyReady={applyReady} onUndo={field => setUndoRequest({id:c.id,title:c.title,field})} onApply={async (label) => {
                                  setApplyingPolicyIds(prev => new Set(prev).add(c.id));
                                  try {
                                      await applyWithSourceConfirmation(f, c.title, message => window.confirm(message),
                                          () => handleApplyField(c.id, label, false, false, {
                                              policyTitle: c.title, oldValue: f.old, newValue: f.styled || f.new,
                                          }));
                                  } finally {
                                      setApplyingPolicyIds(prev => {
                                          const next = new Set(prev);
                                          next.delete(c.id);
                                          return next;
                                      });
                                  }
                              }}
                                  checked={checkedFields.has(`${c.id}::${f.field}`)}
                                  onToggleCheck={() => toggleFieldChecked(c.id, f.field)}
                                  cardBusy={actionBusy} />
                          );
                          return (
                            <div key={c.id} id={`policy-review-${c.id}`} tabIndex={-1} style={{ scrollMarginTop: '24px' }}
                                className={`bg-white dark:bg-[#1e2128] rounded-xl border p-4 ${im.border}`}>
                                <div className="flex items-center gap-2 mb-3 flex-wrap">
                                    {reviewSelectionOpen && !completed && <input type="checkbox" aria-label={`${c.title} 반영 없이 검토 종료 대상으로 선택`} disabled={actionBusy} checked={checkedIds.has(c.id)} onChange={() => toggleChecked(c.id)}
                                        className="w-4 h-4 accent-indigo-600" />}
                                    <button type="button" aria-expanded={expandedPolicies.has(c.id)} aria-controls={`policy-detail-${c.id}`}
                                        onClick={() => setExpandedPolicies(previous => {
                                            const next = new Set(previous);
                                            next.has(c.id) ? next.delete(c.id) : next.add(c.id);
                                            return next;
                                        })}
                                        className="flex-1 min-w-0 text-left font-medium text-gray-800 dark:text-gray-100 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500">
                                        <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1"><span className="break-words text-base font-semibold">{c.title}</span>{completed && <span className={`shrink-0 rounded-full px-2 py-1 text-xs font-medium ${historyNeedsVerification ? 'bg-amber-50 text-amber-700' : 'bg-emerald-50 text-emerald-700'}`}>{historyNeedsVerification ? '반영 확인 필요' : '반영 완료'}</span>}{!completed && reviewLabel && <span className="shrink-0 text-xs font-medium text-amber-700">{reviewLabel}</span>}{fieldGroups.proposalCount > 0 && <span className={`shrink-0 text-xs font-medium px-2 py-0.5 rounded-full ${im.badge}`}>{im.label}</span>}</span>
                                        <span className="block mt-1 text-xs font-normal text-gray-500">{completed ? '반영 내역과 적용 취소를 확인할 수 있습니다.' : `변경 제안 ${fieldGroups.proposalCount}개`}{fieldGroups.unavailable.length > 0 ? ` · 추가 제안 없는 항목 ${fieldGroups.unavailable.length}개` : ''} · {expandedPolicies.has(c.id) ? '상세 접기' : '상세 보기'}{proposedFields.some(f => f.requires_individual_review) ? ' · 개별 확인 필요' : ''}{selectedCount(proposedFields) > 0 ? ` · 선택한 내용 ${selectedCount(proposedFields)}개` : ''}</span>
                                    </button>
                                    {c.deeplink && (
                                        <Tooltip content="Notion에서 열기">
                                            <button onClick={() => openLink(c.deeplink)}
                                                className="text-indigo-400 hover:text-indigo-600 flex items-center gap-0.5 text-xs">
                                                <ExternalLink size={13} /> Notion
                                            </button>
                                        </Tooltip>
                                    )}
                                    {c.source_urls?.map((u, i) => (
                                        <Tooltip key={i} content={`출처 ${c.source_urls.length > 1 ? i + 1 : ''} 원문 보기`}>
                                            <button onClick={() => openLink(u)}
                                                className="text-gray-400 hover:text-blue-500 flex items-center gap-0.5 text-xs">
                                                <ExternalLink size={13} /> 출처{c.source_urls.length > 1 ? i + 1 : ''}
                                            </button>
                                        </Tooltip>
                                    ))}
                                </div>
                                {expandedPolicies.has(c.id) && <div id={`policy-detail-${c.id}`}>
                                {(policyRecoveryRequired(c) || reviewNotes.length > 0) && <div className="mb-3 text-xs leading-relaxed text-amber-800 dark:text-amber-200">
                                    {policyRecoveryRequired(c) && <p>아래 반영·복구 기록과 Notion의 현재 내용을 먼저 확인해주세요. 확인 전에는 추가로 반영할 수 없습니다.</p>}
                                    {reviewNotes.map((note, index) => <p key={index} className="mt-1">{note.note}{note.nextStep && <> · 다음 단계: {note.nextStep}</>}</p>)}
                                </div>}
                                {c.dead_links?.length > 0 && (
                                    <div className="mb-2 text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
                                        <Link2Off size={13} /> 깨진 출처 링크 {c.dead_links.length}건
                                    </div>
                                )}
                                <div className="space-y-5">
                                    {fieldGroups.priority.map(renderField)}
                                    {fieldGroups.minor.length > 0 && <details data-policy-field-group="minor" className="border-t border-gray-100 pt-4 dark:border-gray-700">
                                        <summary className="cursor-pointer text-sm font-medium text-gray-600 dark:text-gray-300">사소한 변경 {fieldGroups.minor.length}개{selectedCount(fieldGroups.minor) > 0 && <span className="ml-2 text-indigo-600 dark:text-indigo-300">· 선택한 내용 {selectedCount(fieldGroups.minor)}개</span>}</summary>
                                        <div className="mt-4 space-y-4">{fieldGroups.minor.map(renderField)}</div>
                                    </details>}
                                    {fieldGroups.unavailable.length > 0 && <details data-policy-field-group="unavailable" className="border-t border-gray-100 pt-4 dark:border-gray-700">
                                        <summary className="cursor-pointer text-sm text-gray-500 dark:text-gray-400">추가 제안 없는 항목 {fieldGroups.unavailable.length}개</summary>
                                        <p className="mt-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400">원문에서 유효한 제안을 얻지 못한 항목입니다. 변경이 없다고 확인된 것은 아니며, 현재 Notion 내용은 변경하지 않습니다.</p>
                                        <dl className="mt-3 space-y-3 text-sm">
                                            {fieldGroups.unavailable.map(f => <div key={f.field}>
                                                <dt className="font-medium text-gray-600 dark:text-gray-300">{f.field}</dt>
                                                <dd className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-gray-500 dark:text-gray-400">{f.snapshot_current === true ? '점검 시 Notion 내용' : '이전에 저장된 비교 내용'}: {f.old || '(없음)'}</dd>
                                                {(f.validation_warning || f.consistency_note) && <dd className="mt-1 text-xs text-amber-700 dark:text-amber-300">{f.validation_warning || f.consistency_note}</dd>}
                                            </div>)}
                                        </dl>
                                    </details>}
                                    {fieldGroups.applied.length > 0 && <details data-policy-field-group="applied" className="border-t border-gray-100 pt-4 dark:border-gray-700">
                                        <summary className="cursor-pointer text-sm text-gray-500 dark:text-gray-400">{historyNeedsVerification ? '반영·복구 기록' : '반영 완료'} {fieldGroups.applied.length}개</summary>
                                        <div className="mt-4 space-y-4">{fieldGroups.applied.map(renderField)}</div>
                                    </details>}
                                </div>
                                <SourceDiffView sd={c.source_diff} />
                                <div className="flex gap-2 mt-3">
                                    {!completed && <Tooltip content={applyingPolicyIds.has(c.id)
                                        ? "이 정책의 필드 적용이 끝난 후 다시 시도해주세요"
                                        : "Notion에는 반영하지 않고, 검토가 끝났다고 로컬에만 표시합니다"}>
                                        {/* (2026-08-28, GPT Codex 지적) 필드 적용 중에 처리 완료를 누르면
                                            적용 작업이 자기가 예전에 읽은 pending_diff로 되살릴 수 있는
                                            경합이 있었다 — 백엔드도 같은 락을 타게 고쳤지만(set_status_locked),
                                            프론트에서도 카드 단위로 함께 잠가 사용자가 굳이 대기하게 만든다. */}
                                        <button onClick={() => handleConfirm(c.id, completed)} disabled={actionBusy}
                                            className="min-h-10 px-3 py-2 text-sm font-medium border border-gray-300 bg-white text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 flex items-center gap-1.5">
                                            이번 제안 반영하지 않기
                                        </button>
                                    </Tooltip>}
                                </div>
                                </div>}
                            </div>
                          );
                        })}
                        {changes.length > displayLimit && <button type="button" onClick={() => setDisplayLimit(limit => limit + 20)} className="w-full rounded-xl border bg-white px-4 py-3 text-sm text-indigo-700">변경 제안 20건 더 보기 ({displayLimit}/{changes.length}건 표시)</button>}
                    </div>
                )}
            </section>

            </section>
            )}

            {pageTab === 'candidates' && (
            <section id="policy-panel-candidates" role="tabpanel" aria-labelledby="policy-tab-candidates" className="space-y-5">
                <div>
                    <h2 id="policy-candidates-heading" className="text-xl font-semibold text-gray-800 dark:text-gray-100">정책 등록 후보</h2>
                    <p className="mt-2 text-sm text-gray-500">새로 등록할 정책을 검토합니다. 지원 대상·혜택·신청 조건은 원문에서 확인해주세요.</p>
                </div>
                <fieldset className="min-w-0">
                    <legend className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">비교할 정책표</legend>
                    <div className="flex flex-wrap gap-2">
                        {displayDbs.map(db => <button key={db.key} type="button" aria-pressed={selected.has(db.key)}
                            disabled={selectionBusy || urlDraftLocked} onClick={() => toggleDb(db.key)}
                            className={`rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50 ${selected.has(db.key)
                                ? 'bg-indigo-100 border-indigo-300 text-indigo-900 dark:bg-indigo-950 dark:border-indigo-700 dark:text-indigo-100'
                                : 'border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300'}`}>{db.name}</button>)}
                    </div>

                    {dbsQ.isError && <p role="alert" className="mt-2 text-sm text-red-700">정책표를 불러오지 못했습니다. <button type="button" onClick={() => dbsQ.refetch()} className="underline">다시 불러오기</button></p>}
                </fieldset>
            {/* URL 직접 초안 — 분석과 Notion 생성을 분리해 확인 전에는 외부 데이터를 바꾸지 않는다. */}
            {urlDraftCreated && <div role="status" aria-label="정책 추가 후 다음 단계" className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm leading-6 dark:border-emerald-900 dark:bg-emerald-950/20">
                <p>‘{urlDraftCreated.title}’ 초안을 Notion에 추가했습니다.</p>
                {urlDraftCreated.warning && <p>{urlDraftCreated.warning}</p>}
                {urlDraftCreated.documentSource
                    ? <p>문서 초안은 Notion에 등록했지만, 정책 최신화는 PDF·이미지 원문을 자동으로 다시 분석하지 않습니다. 변경 내용은 원문과 Notion을 직접 비교해주세요. 자동 비교가 필요하면 Notion의 문서 출처 주소를 해당 정책의 HTML 안내 페이지로 바꾼 뒤 점검하세요. 파일 전송 동의는 이번 초안 분석에만 사용했습니다.</p>
                    : <p>정책 최신화 화면에서 ‘정책 정보 최신인지 확인하기’를 눌러 첫 비교를 시작하세요. 변경 제안은 확인한 항목만 반영합니다.</p>}
                <button type="button" onClick={() => {
                    if (displayDbs.some(db => db.key === urlDraftCreated.db_key)) {
                        setSelected(previous => new Set([...previous, urlDraftCreated.db_key]));
                    }
                    setPageTab('main');
                }} className="min-h-10 font-semibold text-emerald-800 underline underline-offset-2 dark:text-emerald-300">정책 최신화로 이동</button>
            </div>}
            <section className="bg-white dark:bg-[#1e2128] rounded-2xl border border-indigo-100 dark:border-indigo-900/40 overflow-hidden shadow-sm">
                <button type="button" onClick={() => setUrlDraftOpen(open => !open)}
                    aria-expanded={urlDraftOpen} aria-controls="policy-url-draft-panel"
                    className="w-full px-5 py-4 flex items-center gap-3 text-left hover:bg-indigo-50/40 dark:hover:bg-indigo-950/10 transition-colors">
                    <span className="w-10 h-10 rounded-xl bg-indigo-50 text-indigo-600 dark:bg-indigo-900/30 dark:text-indigo-300 flex items-center justify-center">
                        <Link2 size={20} />
                    </span>
                    <span className="flex-1 min-w-0">
                        <span className="font-semibold text-gray-800 dark:text-gray-100 flex items-center gap-2 flex-wrap">
                            직접 찾은 정책으로 초안 만들기
                            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">AI 사용</span>
                        </span>

                    </span>
                    {urlDraftOpen ? <ChevronUp size={18} className="text-gray-400" /> : <ChevronDown size={18} className="text-gray-400" />}
                </button>

                {urlDraftOpen && (
                    <div id="policy-url-draft-panel" className="border-t border-gray-100 dark:border-gray-700 px-5 py-5 space-y-5">
                        <p role={urlDraftPreviewFailed?'status':undefined} className="text-sm text-gray-500 dark:text-gray-400">{urlDraftPreviewFailed?'초안 생성이 계속 실패하면 연결한 Notion 정책표에 사업명·출처 주소(관련 홈페이지 1)·확인한 내용을 직접 입력한 뒤 정책 최신화를 실행하세요. 지금 입력한 주소는 유지됩니다.':'추가할 정책의 웹주소를 넣으면 AI가 초안을 만듭니다. 내용을 확인한 뒤 Notion에 저장하세요.'}</p>
                        {!displayDbs.length ? (
                            <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
                                먼저 <Link to="/settings?tab=guide&step=policy" className="font-semibold underline">정책 최신화 준비하기</Link>에서 정책표를 연결해주세요.
                            </div>
                        ) : (
                            <form onSubmit={handleUrlDraftPreview} className="space-y-3">
                                <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_220px] gap-3">
                                    <label className="text-sm text-gray-600 dark:text-gray-300">
                                        <span className="block mb-1.5 font-medium">정책 안내 페이지 주소</span>
                                        <input type="url" value={urlDraftInput.url}
                                            disabled={urlDraftCreateBusy || urlDraftOperation.current?.kind === 'retry'}
                                            onChange={e => changeUrlDraftInput('url',e.target.value)}
                                            placeholder="https://기관주소.go.kr/정책-상세페이지"
                                            className="w-full px-3 py-2.5 rounded-xl border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-200 dark:focus:ring-indigo-800" />
                                    </label>
                                    <label className="text-sm text-gray-600 dark:text-gray-300">
                                        <span className="block mb-1.5 font-medium">저장할 보관함</span>
                                        <select value={urlDraftInput.dbKey} aria-label="저장할 보관함"
                                            disabled={urlDraftCreateBusy || urlDraftOperation.current?.kind === 'retry'}
                                            onChange={e => changeUrlDraftInput('dbKey',e.target.value)}
                                            className="w-full px-3 py-2.5 rounded-xl border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100">
                                            {displayDbs.map(db => <option key={db.key} value={db.key}>{db.name}</option>)}
                                        </select>
                                    </label>
                                </div>
                                <label className="block text-sm text-gray-600 dark:text-gray-300">
                                    <span className="block mb-1.5 font-medium">정책명 <span className="font-normal text-gray-400">(선택)</span></span>
                                    <input type="text" value={urlDraftInput.title}
                                        disabled={urlDraftCreateBusy || urlDraftOperation.current?.kind === 'retry'}
                                        onChange={e => changeUrlDraftInput('title',e.target.value)}
                                        placeholder="비워두면 웹페이지에서 자동으로 찾습니다"
                                        className="w-full px-3 py-2.5 rounded-xl border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-200 dark:focus:ring-indigo-800" />
                                </label>
                                {urlDraftIsDocument && <div className="space-y-2 text-sm text-gray-600 dark:text-gray-300">
                                    <p>이미지·PDF 안의 개인정보는 자동으로 확인되지 않습니다. 원문 파일을 직접 확인해주세요. 주소가 다른 파일로 연결되면 이동한 파일도 확인해야 합니다.</p>
                                    <button type="button" onClick={() => openLink(urlDraftInput.url.trim())} className="underline underline-offset-4">원문 파일 확인하기</button>
                                    <label className="flex min-h-[44px] items-start gap-2 py-2 cursor-pointer">
                                        <input type="checkbox" checked={urlDraftPrivacyReviewed} disabled={urlDraftLocked}
                                            onChange={e => setUrlDraftPrivacyReviewed(e.target.checked)} className="mt-0.5 h-5 w-5 shrink-0 accent-indigo-600" />
                                        <span>개인정보·비밀키가 없는 공개문서임을 직접 확인했으며, 이 파일을 Google AI에 전송하는 데 동의합니다.</span>
                                    </label>
                                    <p className="text-xs">이 확인은 자동 익명화나 개인정보 안전성 인증이 아닙니다. 이번 미리보기 요청에만 적용됩니다.</p>
                                </div>}
                                <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                                    <p className="text-xs text-gray-400">이 단계에서는 원문을 읽고 미리보기만 만듭니다. Notion은 아직 바뀌지 않습니다.</p>
                                    <button type="submit" disabled={urlDraftLocked || aiStatus?.busy || (urlDraftIsDocument && !urlDraftPrivacyReviewed)}
                                        className="px-4 py-2.5 rounded-xl bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2 shadow-sm">
                                        {urlDraftBusy ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />}
                                        {urlDraftBusy ? '정책 내용 분석 중...' : '정책 초안 만들기'}
                                    </button>
                                </div>
                            </form>
                        )}

                        {urlDraftPreview && (
                            <fieldset disabled={urlDraftLocked} aria-label="정책 초안 검토" className="min-w-0 rounded-2xl border border-indigo-100 dark:border-indigo-900/50 bg-indigo-50/30 dark:bg-indigo-950/10 p-4 space-y-4">
                                <div className="flex items-start gap-3">
                                    <FileText size={20} className="text-indigo-500 mt-0.5 shrink-0" />
                                    <div className="flex-1 min-w-0">
                                        <div className="font-semibold text-gray-800 dark:text-gray-100">AI가 만든 초안입니다</div>
                                        <p className="text-sm text-gray-500 mt-0.5">틀리거나 비어 있는 곳을 고친 뒤 Notion에 추가해주세요. 빈 항목은 비워둔 채 저장할 수 있습니다.</p>
                                        <button type="button" onClick={() => openLink(urlDraftPreview.url)}
                                            className="mt-1.5 text-xs text-indigo-500 hover:text-indigo-700 inline-flex items-center gap-1">
                                            <ExternalLink size={12} /> 분석한 원문 열기
                                        </button>
                                    </div>
                                    <button type="button" onClick={clearUrlDraftPreview}
                                        className="p-1 text-gray-400 hover:text-gray-600" aria-label="미리보기 닫기"><X size={17} /></button>
                                </div>

                                <section aria-label="원문 대조 필요" className="rounded-xl border border-amber-200 bg-amber-50/80 p-3.5 text-sm text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200">
                                    <h3 className="flex items-center gap-2 font-semibold"><AlertTriangle size={17} className="shrink-0" /> 원문 대조 필요</h3>
                                    <p className="mt-1.5 leading-6">AI가 읽거나 정리한 내용에는 누락·오류가 있을 수 있습니다. 원본 웹페이지·파일을 열어 비용, 대상, 접수·이용 기간과 연락처를 확인해주세요.</p>
                                    {urlDraftSourceReview.fields.length > 0 && <p className="mt-1.5 leading-6">특히 대조할 항목: {urlDraftSourceReview.fields.join(' · ')}</p>}
                                    {draftIncomeReviewRequired(urlDraftPreview.source_review) && <p className="mt-1.5 leading-6">소득 기준이 여러 개입니다. 급여·증명서 발급 등 각 기준의 적용 대상과 목적을 확인해주세요. 하나의 기준으로 합치지 않았습니다.</p>}
                                    {urlDraftCoverageNotice&&<p className="mt-1.5 leading-6">{urlDraftCoverageNotice}</p>}
                                    {urlDraftSourceReview.sourceUnavailable ? (
                                        <p className="mt-2 text-xs leading-5">이 미리보기에는 대조용 문장을 표시할 수 없습니다. 위의 ‘분석한 원문 열기’에서 원본을 직접 확인해주세요.</p>
                                    ) : (
                                        <details className="mt-2">
                                            <summary className="min-h-[44px] cursor-pointer py-2.5 font-medium underline underline-offset-4">AI가 읽은 문장 · 원본과 대조해주세요</summary>
                                            <p className="mb-2 text-xs leading-5">아래 문장은 원본 이미지·문서를 대신하지 않습니다. 잘못 읽거나 빠뜨린 부분이 있을 수 있습니다.</p>
                                            <div className="max-h-72 overflow-auto rounded-lg border border-amber-200/80 bg-white/80 p-3 text-gray-700 dark:border-amber-900/50 dark:bg-gray-900/50 dark:text-gray-200" role="region" aria-label="AI가 읽은 문장" tabIndex={0}>
                                                <p className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{urlDraftSourceReview.sourceText}</p>
                                            </div>
                                        </details>
                                    )}
                                </section>

                                {urlDraftMissing.length > 0 && (
                                    <div className="rounded-xl bg-amber-50 border border-amber-100 px-3 py-2.5 text-sm text-amber-800 dark:bg-amber-950/20 dark:border-amber-900/40 dark:text-amber-200">
                                        <b>비어 있는 항목 {urlDraftMissing.length}개</b>
                                        <span className="block text-xs mt-1">정책에 해당하지 않는 항목은 그대로 두어도 됩니다.</span>
                                        <span className="block text-xs mt-1 leading-5">{urlDraftMissing.join(' · ')}</span>
                                    </div>
                                )}

                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <label className="text-sm text-gray-600 dark:text-gray-300 md:col-span-2">
                                        <span className="block mb-1 font-medium">사업명 <span className="text-red-500">*</span></span>
                                        <input value={urlDraftPreview.fields.title || ''}
                                            onChange={e => updateUrlDraftField('title', e.target.value)}
                                            className="w-full px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100" />
                                    </label>
                                    <label className="text-sm text-gray-600 dark:text-gray-300">
                                        <span className="block mb-1 font-medium">분류</span>
                                        {(urlDraftPreview.options?.category || []).length ? (
                                            <select value={urlDraftPreview.fields.category || ''}
                                                onChange={e => updateUrlDraftField('category', e.target.value)}
                                                className="w-full px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100">
                                                <option value="">선택하지 않음</option>
                                                {urlDraftPreview.options.category.map(value => <option key={value} value={value}>{value}</option>)}
                                            </select>
                                        ) : (
                                            <input value={urlDraftPreview.fields.category || ''} readOnly
                                                className="w-full px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-900 text-gray-500" />
                                        )}
                                    </label>
                                    <div className="grid grid-cols-2 gap-2">
                                        {[['start_age_months', '시작 월령'], ['end_age_months', '종료 월령']].map(([key, label]) => (
                                            <label key={key} className="text-sm text-gray-600 dark:text-gray-300">
                                                <span className="block mb-1 font-medium">{label}</span>
                                                <input type="number" min="0" max="1800" value={urlDraftPreview.fields[key] ?? ''}
                                                    onChange={e => updateUrlDraftField(key, e.target.value === '' ? null : Number(e.target.value))}
                                                    placeholder="개월"
                                                    className="w-full px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100" />
                                            </label>
                                        ))}
                                    </div>
                                </div>

                                {URL_DRAFT_MULTI_FIELDS.map(([key, label]) => {
                                    const options = urlDraftPreview.options?.[key] || [];
                                    const selectedValues = urlDraftPreview.fields?.[key] || [];
                                    return (
                                        <div key={key}>
                                            <div className="text-sm font-medium text-gray-600 dark:text-gray-300 mb-1.5">{label}</div>
                                            {options.length ? (
                                                <div className="flex flex-wrap gap-1.5">
                                                    {options.map(value => {
                                                        const selectedValue = selectedValues.includes(value);
                                                        return <button key={value} type="button" onClick={() => toggleUrlDraftOption(key, value)}
                                                            className={`px-2.5 py-1.5 rounded-full text-xs border transition-colors ${selectedValue
                                                                ? 'bg-indigo-600 text-white border-indigo-600'
                                                                : 'bg-white dark:bg-gray-800 text-gray-500 border-gray-200 dark:border-gray-600 hover:border-indigo-300'}`}>
                                                            {value}
                                                        </button>;
                                                    })}
                                                </div>
                                            ) : <p className="text-xs text-gray-400">이 보관함에 등록된 선택지가 없습니다.</p>}
                                        </div>
                                    );
                                })}

                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    {URL_DRAFT_TEXT_FIELDS.map(([key, label, rows]) => (
                                        <label key={key} className={`text-sm text-gray-600 dark:text-gray-300 ${key === 'support_detail' ? 'md:col-span-2' : ''}`}>
                                            <span className="block mb-1 font-medium">{label}</span>
                                            <textarea rows={rows} aria-label={label} value={urlDraftPreview.fields[key] || ''}
                                                onChange={e => updateUrlDraftField(key, e.target.value)}
                                                className="w-full px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100 resize-y" />
                                        </label>
                                    ))}
                                </div>

                                <fieldset className="min-w-0 border-t border-indigo-100 pt-3 dark:border-indigo-900/40" aria-describedby="policy-draft-source-check-help">
                                    <legend className="px-1 text-sm font-semibold text-gray-800 dark:text-gray-100">등록 전 원문 대조 확인</legend>
                                    <p id="policy-draft-source-check-help" className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">금액·대상·접수/이용 기간·연락처를 확인해주세요. 초안을 수정하면 다시 확인해야 합니다.</p>
                                    <div className="mt-2">
                                        {draftSourceReviewItems.map(({key, label, detail}) => <label key={key} className="flex min-h-[44px] cursor-pointer items-start gap-2 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800">
                                            <input type="checkbox" checked={urlDraftSourceChecks[key]} disabled={urlDraftLocked}
                                                onChange={event => {
                                                    if (urlDraftOperation.current) return;
                                                    const next = updateDraftSourceCheck(urlDraftSourceChecksRef.current, key, event.target.checked);
                                                    urlDraftSourceChecksRef.current = next;
                                                    setUrlDraftSourceChecks(next);
                                                }} className="mt-0.5 h-5 w-5 shrink-0 accent-indigo-600" />
                                            <span className="text-sm text-gray-700 dark:text-gray-200"><span className="block font-medium">{label}</span><span className="mt-0.5 block text-xs leading-5 text-gray-500 dark:text-gray-400">{detail}</span></span>
                                        </label>)}
                                    </div>
                                    <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">{urlDraftAgeError || (urlDraftSourceConfirmed ? '확인한 내용으로 등록할 수 있습니다.' : '원문 확인 후 등록할 수 있습니다.')}</p>
                                </fieldset>

                                <div className="flex flex-wrap justify-end gap-2 pt-2 border-t border-indigo-100 dark:border-indigo-900/40">
                                    <button type="button" onClick={clearUrlDraftPreview}
                                        className="px-4 py-2.5 rounded-xl border border-gray-200 dark:border-gray-600 text-sm text-gray-600 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-800">
                                        다시 분석하기
                                    </button>
                                    <button type="button" onClick={handleUrlDraftCreate} disabled={urlDraftLocked || urlDraftNeedsCheck || !urlDraftSourceConfirmed || Boolean(urlDraftAgeError) || !urlDraftPreview.fields.title?.trim()}
                                        className="px-4 py-2.5 rounded-xl bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 flex items-center gap-2 shadow-sm">
                                        {urlDraftCreateBusy ? <Loader2 size={16} className="animate-spin" /> : <FilePlus2 size={16} />}
                                        확인하고 Notion에 추가
                                    </button>
                                </div>
                            </fieldset>
                        )}
                        <div data-url-draft-recovery className="space-y-2 text-sm text-gray-600 dark:text-gray-300">
                            <button type="button" onClick={handleUrlDraftStatus}
                                disabled={urlDraftLocked || !urlDraftInput.url.trim()}
                                className="text-indigo-600 underline disabled:opacity-50 dark:text-indigo-300">
                                {urlDraftStatusBusy ? '등록 상태 확인 중…' : 'Notion 등록 상태 확인'}
                            </button>
                            {urlDraftStatus && <div role="status" className="space-y-2">
                                <p>{urlDraftStatus.message}</p>
                                <p className="break-all text-xs text-gray-500 dark:text-gray-400">확인할 출처: {urlDraftStatus.url}</p>
                                <div className="flex flex-wrap gap-x-4 gap-y-2">
                                    {urlDraftStatus.target_deeplink && <a href={urlDraftStatus.target_deeplink} target="_blank" rel="noopener noreferrer" className="underline">원래 정책표 열기</a>}
                                    {urlDraftStatus.deeplink && <a href={urlDraftStatus.deeplink} target="_blank" rel="noopener noreferrer" className="underline">등록된 Notion 페이지 열기</a>}
                                    {urlDraftStatus.can_retry && urlDraftStatus.attempt_id && <button type="button" disabled={urlDraftLocked}
                                        onClick={handleUrlDraftRetry} className="underline disabled:opacity-50">Notion 미등록 확인 후 재시도 허용</button>}
                                </div>
                            </div>}
                        </div>
                    </div>
                )}
            </section>

            {/* 신규 후보 섹션 */}
            <section aria-labelledby="collected-policy-candidates" className="border-t border-gray-200 pt-5 dark:border-gray-700">
                <h3 id="collected-policy-candidates" className="text-base font-semibold text-gray-800 dark:text-gray-100 mb-2">수집 자료에서 찾은 후보</h3>
                <p className="mb-4 text-xs text-gray-500">‘Notion에 초안 추가’를 누르면 선택한 정책표에 바로 저장됩니다. 정책으로 확정된 자료는 아닙니다.</p>
                {candsQ.isError ? (
                    <p role="alert" className="text-red-700">추가 후보를 불러오지 못했습니다. <button onClick={() => candsQ.refetch()} className="underline">다시 불러오기</button></p>
                ) : candsQ.isLoading ? (
                    <div className="p-4 text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 로딩 중...</div>
                ) : candidates.length === 0 ? (
                    <div className="p-6 text-center text-gray-400 bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 text-sm">
                        {!dbKeys.length ? '비교할 정책표를 선택해주세요.' : '정책표에 없는 신규 후보가 없습니다.'}
                    </div>
                ) : (
                    <div className="space-y-2">
                        {candidates.map(it => (
                            <div key={it.id} className="bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 p-4">
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="text-base font-semibold text-gray-800 dark:text-gray-100">{it.title}</span>
                                    {it.similar && (
                                      <Tooltip content={`유사: ${it.similar.title} (${it.similar.score})`}>
                                        <span
                                            className="text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300 flex items-center gap-1">
                                            <AlertTriangle size={12} /> 유사정책 ({it.similar.score})
                                        </span>
                                      </Tooltip>
                                    )}
                                    <button onClick={() => openLink(it.link)} className="text-gray-400 hover:text-indigo-500">
                                        <ExternalLink size={13} />
                                    </button>
                                </div>
                                <div className="text-xs text-gray-500 mt-1 flex gap-3 flex-wrap">
                                    <span className="flex items-center gap-1"><Building2 size={12} /> {it.site_name || '-'}</span>
                                    <span className="flex items-center gap-1"><Tag size={12} /> {it.category || '-'}</span>
                                    {it.target && <span className="flex items-center gap-1"><Target size={12} /> {it.target}</span>}
                                </div>
                                <div className="flex items-center gap-2 mt-3 flex-wrap">
                                    <select aria-label={`${it.title} 저장할 정책표`} value={draftPick[it.id] || it.similar?.db_key || displayDbs[0]?.key || ''}
                                        disabled={!displayDbs.length || urlDraftLocked || dismissingCandidateIds.has(it.id)}
                                        onChange={e => setDraftPick(p => ({ ...p, [it.id]: e.target.value }))}
                                        className="px-2 py-1.5 text-sm border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100">
                                        {displayDbs.map(d => <option key={d.key} value={d.key}>{d.name}</option>)}
                                    </select>
                                    <button onClick={() => handleDraft(it)} disabled={!displayDbs.length || urlDraftLocked || dismissingCandidateIds.has(it.id) || (urlDraftNeedsCheck && urlDraftStatus.url === it.link)}
                                        className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-1.5">
                                        <FilePlus2 size={15} /> Notion에 초안 추가
                                    </button>
                                    <button onClick={() => handleDismissCandidate(it.id)} disabled={urlDraftLocked || dismissingCandidateIds.has(it.id)}
                                        className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800">
                                        {dismissingCandidateIds.has(it.id) ? '제외 중…' : '무시'}
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </section>
            </section>
            )}
            {pageTab === 'main' && (visibleFields.length > 0 || visibleIds.length > 0 || bulkResult) && (
                <div role="region" aria-label="선택한 정책 작업" className="sticky bottom-3 z-20 rounded-2xl border border-indigo-200 bg-white dark:bg-gray-900 p-4 shadow-lg space-y-3">
                    {bulkResult && <div role="status" className="text-sm">
                        <div className="flex justify-between gap-2"><strong>{bulkResult.summary}</strong><button aria-label="반영 결과 닫기" onClick={() => setBulkResult(null)}>닫기</button></div>
                        {bulkResult.issues.length > 0 && <details className="mt-2"><summary className="cursor-pointer text-amber-800">반영하지 못한 내용 확인 ({bulkResult.issues.length})</summary><ul className="mt-2 max-h-32 overflow-auto space-y-2">{bulkResult.issues.map((item, index) => <li key={index}>{item.title} · {item.field}: {item.message || '자동 반영을 보류했습니다. 카드에서 제안 내용을 개별 확인해주세요.'} <button type="button" disabled={actionBusy} onClick={() => setJumpPolicyId(item.id)} className="ml-2 rounded border border-gray-300 px-2 py-1 text-xs text-blue-700 disabled:opacity-50">카드 보기</button></li>)}</ul></details>}
                    </div>}
                    {<div className="flex flex-wrap items-center gap-3">
                        <span className="text-sm flex-1 min-w-40">반영할 내용 <b>{visibleFields.length}개</b><span className="block text-xs text-gray-500">선택한 내용만 Notion에 반영합니다.</span></span>
                        <button disabled={selectionBusy} onClick={() => { setCheckedFields(new Set()); setCheckedIds(new Set()); }} className="text-sm text-gray-600 underline disabled:opacity-50">선택 해제</button>
                        {visibleIds.length > 0 && <button disabled={actionBusy} onClick={handleBulkConfirm} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-50">{visibleIds.length}건 검토 종료 · 반영 안 함</button>}
                        <button disabled={!visibleFields.length || actionBusy || changesQ.isError} onClick={handleBulkApplyFields} title="임시 선택한 내용을 적용합니다. 원문 확인이 필요한 항목은 하나씩 확인합니다." className="rounded-xl bg-indigo-600 text-white px-4 py-3 text-sm font-semibold disabled:opacity-50">{bulkApplyBusy ? 'Notion에 반영 중…' : `선택한 내용 Notion에 반영 (${visibleFields.length})`}</button>
                    </div>}
                </div>
            )}
        </div>
    );
};

export default PolicyManage;

import React, { useEffect, useState } from 'react';
import { welfareApi } from '../api/welfare';
import { getErrorMessage } from '../utils/error';
import { isRealLink } from '../utils/link';
import { Link } from 'react-router-dom';
import NewsResultDetails from './NewsResultDetails';
import {newsResultPresentation} from '../utils/newsResultPresentation';

const changeLabels = {new:'처음 확인', unchanged:'이전과 같음', updated:'내용 변경'};
export default function NewsProfileResults({ profileId, runId, onClose, actionBusy = false }) {
    const [data, setData] = useState(null), [error, setError] = useState('');
    const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
    const [busy, setBusy] = useState(true);
    useEffect(() => {
        let cancelled = false;
        setBusy(true); setError(''); setData(null);
        welfareApi.getNewsProfileResults(profileId, offset, runId).then(value => {
            if (!cancelled) setData(value);
        }).catch(e => { if (!cancelled) setError(getErrorMessage(e)); })
            .finally(() => { if (!cancelled) setBusy(false); });
        return () => { cancelled = true; };
    }, [profileId, runId, offset, refresh]);
    const button = 'rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm disabled:opacity-50';
    return <section aria-label="저장된 분석 결과" aria-busy={busy} className="rounded-xl border border-gray-200 dark:border-gray-700 p-4 space-y-3 text-gray-800 dark:text-gray-100">
        <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">수집 처리 기록</h3><button className={button} onClick={onClose}>기록 닫기</button></div>
        <p className="text-sm text-gray-600 dark:text-gray-300">보류·제외를 포함한 처리 기록입니다. 분석을 마친 관련 자료는 ‘찾은 소식’에 바로 표시되며, Notion에는 자동 등록하지 않습니다.</p>
        <Link to={`/notion?profile=${encodeURIComponent(profileId)}`} className="inline-block py-2 text-sm text-blue-600 underline">찾은 소식으로 이동</Link>
        <details className="text-sm text-gray-500"><summary className="cursor-pointer">결과 표시 도움말</summary>
            <p>‘검토 필요’는 근거가 부족한 자료이며 제외를 뜻하지 않습니다. ‘추가 분석 대기’는 아직 분석하지 못한 자료입니다.</p>
            <p>‘이미지 글자 활용’은 PC에서 OCR로 읽은 내용을 사용했다는 뜻입니다. 인식 오류가 있을 수 있으므로 신청 전에 날짜·금액·자격 조건을 원문과 비교하세요.</p>
            {data?.items.some(item=>item.source_type==='bokjiro_service')&&<p>이전 수집 기록의 ‘복지로 공식 서비스’는 새 게시물과 다릅니다. 새 소식 찾기는 등록한 기관 사이트만 수집합니다.</p>}
            <p>결과 확인은 AI를 사용하지 않습니다. 재개 가능한 작업은 실행 화면에서 이어갈 수 있습니다.</p>
            <p>저장·등록은 ‘찾은 소식’에서 필요한 항목을 선택해 진행하세요. 별도로 가져오는 단계는 없습니다.</p>
        </details>
        {busy && <p role="status">결과 불러오는 중…</p>}
        {error && <p role="alert" className="text-sm text-red-600 break-words">{error}</p>}
        {data && !data.items.length && <p className="text-sm">이 페이지에 저장된 분석 결과가 없습니다.</p>}
        <ul className="space-y-2">{data?.items.map(item => {
            const presentation = newsResultPresentation(item);
            const evidence = item.review_evidence;
            return <li key={item.record_id || item.job_id} className="rounded-lg bg-gray-50 dark:bg-gray-800 p-3 break-words space-y-1">
            <p className="text-sm font-medium">{item.title || '제목 없음'}</p>
            {item.source_type==='bokjiro_service'&&<p className="text-xs font-medium text-emerald-700 dark:text-emerald-300">복지로 공식 서비스{item.change_state?` · ${changeLabels[item.change_state]||'변경 상태 확인 필요'}`:''}</p>}
            <p className={`text-sm ${item.target_status === 'review' || presentation.needsSourceReview ? 'text-amber-800 dark:text-amber-200' : 'text-gray-600 dark:text-gray-300'}`}>
                {presentation.statusLabel} · 기준 버전 {item.revision}
            </p>
            {item.input_note && <p className="text-sm text-amber-800 dark:text-amber-200 break-words">{item.input_note}</p>}
            {isRealLink(item.link) && <a href={item.link} target="_blank" rel="noopener noreferrer" className="inline-block py-2 text-sm text-blue-600 underline">원문 확인</a>}
            <NewsResultDetails key={`${profileId}:${item.record_id || item.job_id}:${item.revision}:${refresh}`} profileId={profileId} item={item}/>
            {item.target_status === 'review' && evidence?.version === 1 && Array.isArray(evidence.items) && (evidence.items.length > 0 || evidence.omitted_count > 0) &&
                <details className="rounded-lg border border-amber-200 p-3 text-sm dark:border-amber-900">
                    <summary className="cursor-pointer font-medium text-amber-900 dark:text-amber-200">원문 대조 안내</summary>
                    <p className="mt-2 text-gray-600 dark:text-gray-300">AI 분석의 검증 완료나 지원 자격 판정을 뜻하지 않습니다. 자동 반영하지 않으며 원문 전체를 확인해주세요.</p>
                    {evidence.items.slice(0, 3).map((passage, index) => <div key={index} className="mt-3">
                        <p className="font-medium">{passage.label} · 원문 {passage.start_line}–{passage.end_line}행</p>
                        <p className="mt-1 whitespace-pre-wrap break-words">{passage.text}</p>
                    </div>)}
                    {evidence.omitted_count > 0 && <p className="mt-2 text-gray-600 dark:text-gray-300">이 밖의 확인할 내용은 원문에서 직접 확인해주세요.</p>}
                </details>}
        </li>})}</ul>
        <div className="flex flex-wrap gap-2">
            <button className={button} disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>이전 10건</button>
            <button className={button} disabled={busy || !data?.has_more} onClick={() => setOffset(offset + 10)}>다음 10건</button>
            <button className={button} disabled={busy} onClick={() => setRefresh(v => v + 1)}>새로고침</button>
            <button className={button} onClick={onClose}>결과 닫기</button>
        </div>
    </section>;
}

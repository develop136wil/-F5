import React, {useEffect, useState} from 'react';
import {welfareApi} from '../api/welfare';
import {getErrorMessage} from '../utils/error';
import {checkedDetailResponse, detailEntries, detailOmissions} from '../utils/newsResultDetails';
import ReadableMarkdown from './ReadableMarkdown';

export default function NewsResultDetails({profileId, item}) {
    const [open, setOpen] = useState(false), [retry, setRetry] = useState(0);
    const [result, setResult] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const omissions = detailOmissions(item.detail_omissions);
    const needsFetch = omissions.some(field => field.available);
    const recordId = item.record_id || (item.job_id ? `job:${item.job_id}` : '');
    useEffect(() => {
        setResult(null); setError(''); setBusy(false);
        if (!open || !needsFetch || !recordId) return;
        const controller = new AbortController();
        let current = true;
        setBusy(true);
        welfareApi.getNewsResultDetails(profileId, recordId, {signal:controller.signal})
            .then(value => { if (current) setResult(checkedDetailResponse(value, recordId)); })
            .catch(e => { if (current && !controller.signal.aborted) setError(getErrorMessage(e)); })
            .finally(() => { if (current) setBusy(false); });
        return () => { current = false; controller.abort(); };
    }, [profileId, recordId, item, open, needsFetch, retry]);
    if (!detailEntries(item.details).length && !omissions.length) return null;
    const pending = needsFetch && !result;
    const fields = detailEntries(result ? result.details : pending ? {} : item.details);
    const hidden = detailOmissions(result ? result.detail_omissions : item.detail_omissions);
    return <details className="text-sm" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer py-2">분석 내용 보기{omissions.some(field => field.available) ? ' · 긴 내용 포함' : ''}</summary>
        {open && <div className="mt-2 space-y-3" aria-busy={busy}>
            {busy && <p role="status">전체 분석 내용 불러오는 중… AI를 사용하지 않습니다.</p>}
            {error && <div><p role="alert" className="text-red-700 dark:text-red-300 break-words">{error}</p>
                <button className="mt-2 rounded-lg border px-3 py-2 min-h-11" onClick={() => setRetry(value => value + 1)}>다시 불러오기</button></div>}
            {hidden.length > 0 && <ul className="space-y-1 text-gray-600 dark:text-gray-300">{hidden.map(field => <li key={field.key}>
                {field.label} · {field.length.toLocaleString('ko-KR')}자 — {field.available
                    ? '목록에서는 생략한 내용입니다. 전체 내용을 불러옵니다.'
                    : field.reason === 'display_limit' ? '화면 표시 한도를 넘어 생략했습니다. 분석 기록은 유지되며 원문에서 확인해주세요.'
                    : '현재 방식으로 확인할 수 없는 이전 분석 내용입니다. 기록은 유지되며 원문에서 확인해주세요.'}
            </li>)}</ul>}
            {fields.length > 0 && <dl className="space-y-3">{fields.map(field => <div key={field.key}>
                <dt className="font-medium">{field.label}</dt>
                <dd className="mt-1 max-h-96 overflow-y-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere] rounded-md border border-gray-200 p-3 dark:border-gray-700"
                    tabIndex={0} role="region" aria-label={`${field.label} 전체 내용`}><ReadableMarkdown text={field.text}/></dd>
            </div>)}</dl>}
            {!pending && !fields.length && !hidden.length && <p>현재 검증된 표시 내용이 없습니다. 원문과 검토 안내를 확인해주세요.</p>}
        </div>}
    </details>;
}

import React, {useId} from 'react';

export default function NewsPublicationPeriod({value, onChange, disabled = false}) {
    const id = useId();
    return <fieldset disabled={disabled} className="space-y-2 text-sm">
        <legend className="mb-2 font-medium">수집 기간 · 게시일 기준</legend>
        <label htmlFor={id} className="sr-only">수집 기간</label>
        <select id={id} value={value.publication_period || '30'} onChange={e => onChange('publication_period', e.target.value)}
            className="min-h-10 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800">
            <option value="7">최근 7일</option><option value="30">최근 30일</option><option value="90">최근 90일</option><option value="custom">기간 직접 지정</option>
        </select>
        {value.publication_period === 'custom' && <div className="flex flex-wrap gap-3">
            <label className="space-y-1"><span className="block">게시일 시작</span><input type="date" value={value.publication_start_date || ''} onChange={e => onChange('publication_start_date', e.target.value)} className="min-h-10 rounded-lg border border-gray-300 bg-transparent p-2" /></label>
            <label className="space-y-1"><span className="block">게시일 종료</span><input type="date" value={value.publication_end_date || ''} onChange={e => onChange('publication_end_date', e.target.value)} className="min-h-10 rounded-lg border border-gray-300 bg-transparent p-2" /></label>
        </div>}
        <p className="text-xs leading-5 text-gray-500">기관이 글을 게시한 날짜를 기준으로 찾습니다. 신청 마감일·수집일과 다릅니다. 날짜가 불명확하면 따로 표시하며, 기간을 바꿔도 기존 소식은 삭제하지 않습니다.</p>
    </fieldset>;
}

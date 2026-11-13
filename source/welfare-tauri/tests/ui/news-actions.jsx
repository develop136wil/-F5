// No production calls. Counters and controls are visible for browser verification.
import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from 'sonner';
import '../../src/index.css';
import NotionUpload from '../../src/pages/NotionUpload';
import { welfareApi } from '../../src/api/welfare';
import { downloadBlob } from '../../src/utils/download';
import { MemoryRouter } from 'react-router-dom';
import { ScrapeProvider } from '../../src/context/ScrapeContext';
import { queryKeys } from '../../src/api/queryKeys';

const client = new QueryClient({defaultOptions: {queries: {retry: false, refetchOnWindowFocus: false}}});
let rows = [{id: 'item:1', item_id: 1, fingerprint:'1'.padStart(64,'0'), selectable:true, title: '시험용 한부모 가족 지원', input_review_required: true,
    input_note: '페이지 용량 제한으로 일부를 읽지 못했습니다.', status: '진행 중', link: 'https://example.invalid/one'},
    {id: 'item:2', item_id: 2, fingerprint:'2'.padStart(64,'0'), selectable:true, title: '시험용 노년 문화 행사', status: '진행 예정', link: 'https://example.invalid/two', upload_uncertain: true}];
let notify = () => {}, failNext = false, changeNext = false, pending = null;
const counts = {html: 0, upload: 0, dismiss: 0, retry: 0, downloaded: 0};
const response = ({view='ready',offset=0}={}) => {
    const visible = view === 'ready' ? rows : [];
    return {configured:true, view, offset, limit:10, count:visible.length, total:visible.length,
        counts:{ready:rows.length,review:0,records:0}, has_more:offset+10<visible.length, items:visible.slice(offset,offset+10)};
};
const refresh = () => {
    client.setQueriesData({queryKey:queryKeys.newsFeed}, current => current ? response(current) : current);
    notify();
};
for (const key of Object.keys(welfareApi)) {
    if (typeof welfareApi[key] === 'function') welfareApi[key] = async () => { throw Error('격리 시험: 실제 호출 차단'); };
}
welfareApi.getNotionCandidates = async () => response();
welfareApi.getNewsFeed = async (options) => response(options);
welfareApi.prepareNewsFeedAction = async (_profileId, action, selections) => {
    if (!selections.length) throw Error('격리 시험: 빈 선택');
    return {ids:selections.map(selected => {
        const row = rows.find(item => item.id === selected.id && item.fingerprint === selected.fingerprint && item.selectable);
        if (!row) throw Error('격리 시험: 선택이 변경됨');
        if (action === 'retry' && !row.upload_uncertain) throw Error('격리 시험: 불확실 업로드가 아님');
        if (action === 'upload' && row.upload_uncertain) throw Error('격리 시험: 업로드 확인 필요');
        return row.item_id;
    }), action, ai_called:false, notion_uploaded:false};
};
welfareApi.getStatus = async () => ({is_running: false});
welfareApi.getDefaultNewsCondition = async () => ({profile: null,legacy_collection_available:true});
welfareApi.getAiStatus = async () => ({busy: false});
welfareApi.getConfig = async () => ({is_configured: true, TARGET_REGION: '시험 지역', TARGET_AGE_MAX_MONTHS: 108});
welfareApi.getSites = async () => [{id: 1}];
welfareApi.getAiBacklogStatus = async () => ({backlog_total: 0});
welfareApi.exportNewsGallery = async () => {
    counts.html++; notify();
    await new Promise((resolve, reject) => { pending = () => {
        pending = null;
        if (failNext) { failNext = false; notify(); reject(Error('모의 저장 실패')); } else resolve();
    }; notify(); });
    downloadBlob('<!doctype html><html lang="ko"><meta charset="utf-8"><title>시험용 새소식</title><h1>시험용 새소식</h1><p>원문 확인 필요: 일부 내용을 읽지 못했습니다.</p><button onclick="navigator.clipboard.writeText(document.querySelector(\'p\').textContent)">내용 복사</button></html>', 'WelfareF5_News_fixture.html', 'text/html');
    counts.downloaded++; notify();
    return {posters: 0, cacheHits: 1};
};
welfareApi.uploadToNotion = async ids => { counts.upload++; notify(); return {total: ids.length, uploaded: ids.length}; };
welfareApi.dismissNotionCandidates = async ids => { counts.dismiss++; rows = rows.filter(row => !ids.includes(row.item_id)); refresh(); };
welfareApi.allowNotionUploadRetry = async ids => { counts.retry++; rows = rows.map(row => ids.includes(row.item_id) ? {...row,upload_uncertain:false} : row); refresh(); };

function Fixture() {
    const [, update] = useState(0);
    const [show, setShow] = useState(true);
    useEffect(() => {
        notify = () => update(n => n + 1);
        const observer = new MutationObserver(() => {
            if (changeNext && document.querySelector('dialog[open]')) {
                changeNext = false;
                rows = rows.map(row => ({...row, title: `${row.title} (수정됨)`})); refresh();
            }
        });
        observer.observe(document.body, {attributes: true, childList: true, subtree: true});
        return () => { notify = () => {}; observer.disconnect(); };
    }, []);
    return <main className="mx-auto max-w-5xl space-y-6 p-6">
        <aside className="rounded-xl border bg-white p-4 space-y-3" aria-label="시험 제어">
            <p>격리 시험 · 외부 API / 실제 DB / Notion 호출 없음</p>
            <output aria-label="시험 요청 횟수">{JSON.stringify(counts)}</output>
            <div className="flex flex-wrap gap-2">
                <button onClick={() => pending?.()} disabled={!pending}>모의 요청 완료</button>
                <button onClick={() => { failNext = true; notify(); }}>다음 저장 실패</button>
                <button onClick={() => { changeNext = true; notify(); }}>다음 확인 중 목록 변경</button>
                <button onClick={() => { rows = rows.filter(row => row.item_id !== 1); refresh(); }}>첫 항목 목록에서 제거</button>
                <button onClick={() => { rows = rows.map(row => ({...row, upload_uncertain:false})); refresh(); }}>모든 전송 확인 완료</button>
                <button onClick={() => setShow(value => !value)}>시험 화면 전환</button>
            </div>
            <p>{failNext ? '다음 저장: 실패 예정' : '다음 저장: 정상'} / {changeNext ? '확인 중 목록 변경 예정' : '목록 변경 없음'}</p>
        </aside>
        {show && <NotionUpload />}
        <Toaster />
    </main>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={client}><ScrapeProvider><Fixture /></ScrapeProvider></QueryClientProvider></MemoryRouter>);

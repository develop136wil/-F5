import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

// Every endpoint is blocked before importing the real page. All data, delays,
// failures and read/selection records below live only in this browser's memory.
const state = window.searchFixture = {requests: [], details: [], unexpected: [], aborted: [], next: null, displayCases:false};
for (const name of Object.keys(welfareApi)) if (typeof welfareApi[name] === 'function') {
    welfareApi[name] = async () => {state.unexpected.push(name); throw Error(`Blocked API: ${name}`);};
}
const entries = new Map();
const responseFor = request => {
    const {mode, query, category, offset, activeOnly} = request;
    const total = category ? 2 : query === '새검색' ? 3 : mode === 'semantic' ? 140 : 205;
    const base = (mode === 'semantic' ? 10000 : 0) + (category ? 1000 : 0)
        + (query === '새검색' ? 2000 : query === '먼저검색' ? 3000 : 0) + (activeOnly ? 4000 : 0);
    const items = Array.from({length: Math.min(100, total - offset)}, (_, index) => {
        const ordinal = offset + index + 1, id = base + ordinal;
        const item = {id, title: `${mode === 'semantic' ? '통합' : '단어'} ${category || '전체'} ${query || '모든자료'} ${ordinal}번`,
            category: category || (ordinal % 2 ? '교육·보육' : '생활지원'),
            site_name: ordinal % 3 ? '시험 기관' : '여러 기관의 이름이 길게 표시되는 합성 지원기관',
            support_detail: ordinal % 2 ? '짧은 합성 안내입니다.' : '화면 너비와 글자 크기에 따라 줄 수가 늘어나는 합성 안내입니다. '.repeat(8),
            scraped_at: '2026-09-08T09:00:00', link: `https://example.invalid/news/${id}`,
            event_date: ordinal % 3 ? undefined : '2026-10-01'};
        item.news_presentation = {support_detail:item.support_detail,presentation_kind:'legacy',source_note:''};
        if (state.displayCases && ordinal <= 4) {
            item.support_detail = '--- [첨부파일: 본문 이미지] --- 메뉴 COPYRIGHT 숨겨야 하는 원문';
            item.ai_data = {support_detail:item.support_detail};
            if (ordinal === 1) item.news_presentation = {support_detail:'### 지원 내용\n- **교재비 5만원(50,000원)** 지원.\n- 중복 신청은 불가합니다.',presentation_kind:'structured'};
            if (ordinal === 2) item.news_presentation = {support_detail:'',presentation_kind:'source_excerpt',source_note:'원문에서 내용을 확인해주세요.'};
            if (ordinal === 3) item.news_presentation = {support_detail:42};
            if (ordinal === 4) {item.ai_data.item_type='policy';item.support_detail='담당자가 관리한 정책 본문 10만원';}
        }
        entries.set(id, item); return item;
    });
    return {items, total};
};
const request = (mode, query, category, offset, activeOnly, signal) => {
    const entry = {id: state.requests.length, mode, query: query || '', category: category || '', offset, activeOnly, hasSignal: signal instanceof AbortSignal};
    state.requests.push(entry);
    const next = state.next; state.next = null;
    if (next === 'fail') return Promise.reject(Error('합성 검색 오류'));
    if (next === 'defer') return new Promise((resolve, reject) => {
        entry.pending = true;
        const clean = () => {entry.pending = false; signal?.removeEventListener('abort', abort);};
        const abort = () => {
            clean(); entry.aborted = true; state.aborted.push(entry.id);
            reject(Object.assign(Error('합성 요청 취소'), {code: 'ERR_CANCELED'}));
        };
        entry.resolve = () => {clean(); resolve(responseFor(entry));};
        entry.reject = () => {clean(); reject(Error('합성 검색 오류'));};
        if (signal?.aborted) abort();
        else signal?.addEventListener('abort', abort, {once: true});
    });
    return Promise.resolve(responseFor(entry));
};
welfareApi.getSummaryStats = async () => ({total: 205, by_category: {'교육·보육': 103, 생활지원: 102}});
welfareApi.searchItems = (query, category, site, limit, offset, activeOnly, signal) => request('exact', query, category, offset, activeOnly, signal);
welfareApi.semanticSearch = (query, options) => request('semantic', query, options.category, 0, options.activeOnly, options.signal);
welfareApi.getItemDetail = async id => {state.details.push(id); return entries.get(id);};
welfareApi.getSimilarItems = async () => [];
const client = new QueryClient({defaultOptions: {queries: {retry: false, refetchOnWindowFocus: false}}});
state.refresh = () => client.invalidateQueries({predicate: q => q.queryKey[0] !== 'summaryStats'});
const {default: Search} = await import('../../src/pages/Search');
const root = createRoot(document.getElementById('root'));
state.unmount = () => root.unmount();
root.render(<QueryClientProvider client={client}>
    <main className="p-4"><Search/></main>
</QueryClientProvider>);

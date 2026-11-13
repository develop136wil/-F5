import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {Toaster} from 'sonner';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

// Entirely synthetic, memory-only fixture. Every unlisted API is prohibited.
const fixture = window.pageUxFixture = {calls: [], unexpected: [], failSearch: true};
for (const name of Object.keys(welfareApi)) {
    if (typeof welfareApi[name] === 'function') welfareApi[name] = async () => {
        fixture.unexpected.push(name);
        throw Error(`Blocked API: ${name}`);
    };
}
welfareApi.getSummaryStats = async () => ({total: 0, by_category: {}, by_site: {}});
welfareApi.getSites = async () => [];
welfareApi.getTrends = async () => [];
welfareApi.getSitesHealth = async () => [];
welfareApi.getBatchStatus = async () => ({is_running: false});
welfareApi.getAiStatus = async () => ({busy: false});
const search = async query => {
    fixture.calls.push({name: 'search', query});
    if (fixture.failSearch) throw Error('시험용 검색 오류');
    return {items: [{id: 'fixture', title: '시험 복지 지원 소식', category: '생활지원',
        support_detail: '시험용 안내입니다.', link: 'https://example.invalid/news',
        scraped_at: '2026-09-08T00:00:00'}], total: 1};
};
welfareApi.semanticSearch = search;
welfareApi.searchItems = search;
const pages = {search: () => import('../../src/pages/Search'),
    reports: () => import('../../src/pages/Reports'), sites: () => import('../../src/pages/Sites')};
const kind = new URLSearchParams(location.search).get('page') || 'search';
const {default: Page} = await pages[kind]();
const client = new QueryClient({defaultOptions: {queries: {retry: false, refetchOnWindowFocus: false}}});
createRoot(document.getElementById('root')).render(
    <MemoryRouter><QueryClientProvider client={client}>
        <main className="mx-auto min-h-screen max-w-6xl bg-gray-100 p-4 text-gray-800 dark:bg-gray-950 dark:text-gray-100"><Page /></main>
        <Toaster />
    </QueryClientProvider></MemoryRouter>);

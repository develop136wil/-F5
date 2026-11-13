import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster, toast } from 'sonner';
import { welfareApi } from '../../src/api/welfare';
import '../../src/index.css';

const mode = new URLSearchParams(location.search).get('mode') || 'slow';
const state = window.siteTestFixture = { posts: 0, polls: 0, elapsed: 0, unexpected: [], notifications: [], finished: false, complete: () => { state.finished = true; } };
state.notifyUnrelated = () => toast.info('별도 알림 유지', { id: 'unrelated', duration: Infinity });
const realTimeout = window.setTimeout.bind(window);
// Test-only clock scale: every production 1.5-second poll represents 1.5 s,
// while wall-clock delay is 20 ms. No website/API calls leave this fixture.
window.setTimeout = (fn, ms, ...args) => realTimeout(fn, ms === 1500 || ms === 5000 ? 20 : ms, ...args);
for (const level of ['loading', 'success', 'warning', 'error', 'info']) {
    const original = toast[level];
    toast[level] = (message, options) => { state.notifications.push({ level, message, id: options?.id }); return original(message, options); };
}
for (const name of Object.keys(welfareApi)) if (typeof welfareApi[name] === 'function') welfareApi[name] = async () => { state.unexpected.push(name); throw Error(`Blocked: ${name}`); };
const base = { site_id: 1, task_id: 'synthetic-test', status: 'running', running: true, result: null };
let serverTask = mode === 'recover' ? { ...base } : null;
const site = { id: 1, name: '시험 기관', url: 'https://example.invalid/board', list_selector: '.synthetic', title_selector: '.title', link_selector: 'a' };
welfareApi.getSites = async () => [{ ...site, test_task: serverTask }];
welfareApi.getSitesHealth = async () => [];
welfareApi.getBatchStatus = async () => ({ is_running: false });
welfareApi.getAiStatus = async () => ({ busy: false });
welfareApi.testSite = async () => {
    state.posts += 1;
    if (mode === 'conflict') throw Object.assign(Error('synthetic conflict'), { response: { status: 409 } });
    serverTask = { ...base };
    if (mode === 'post-timeout') throw Object.assign(Error('synthetic timeout'), { code: 'ECONNABORTED' });
    return structuredClone(serverTask);
};
welfareApi.getSiteTestStatus = async () => {
    state.polls += 1; state.elapsed += 1500;
    if (mode === 'deleted') { serverTask = null; throw Object.assign(Error('synthetic deleted'), { response: { status: 404 } }); }
    if (mode === 'transient' && state.polls <= 2) throw Error('synthetic offline');
    if (mode === 'malformed' && state.polls <= 2) return { running: false };
    if (mode === 'idle') return { site_id: 1, task_id: null, status: 'idle', running: false, result: null };
    const waiting = ['navigate', 'recover', 'double'].includes(mode) ? !state.finished : state.polls < (mode === 'slow' ? 18 : 4);
    if (waiting) return structuredClone(serverTask);
    serverTask = { ...base, status: mode === 'timeout' ? 'timed_out' : mode === 'failed' ? 'failed' : 'completed', running: false,
        result: { status: 'success', site: site.name, count: mode === 'zero' ? 0 : 10, items: [{ title: '게시판 fallback 소식' }],
            ...(['verified', 'needs-check'].includes(mode) ? { registration_status: mode === 'verified' ? 'basic_verified' : 'needs_check' } : {}) } };
    return structuredClone(serverTask);
};
const { default: Sites } = await import('../../src/pages/Sites');
const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
state.cachedTask = () => client.getQueryData(['siteTestTask']);
createRoot(document.getElementById('root')).render(<MemoryRouter initialEntries={['/sites']}><QueryClientProvider client={client}>
    <nav><Link to="/away">다른 화면</Link> <Link to="/sites">사이트 화면</Link></nav>
    <Routes><Route path="/sites" element={<Sites />} /><Route path="/away" element={<div>다른 작업 화면</div>} /></Routes><Toaster />
</QueryClientProvider></MemoryRouter>);

// Isolated UI fixture. Every API method is replaced; no production backend/AI.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '../../src/index.css';
import { welfareApi } from '../../src/api/welfare';
import NotionUpload from '../../src/pages/NotionUpload';
import NewsProfileResults from '../../src/components/NewsProfileResults';
import { MemoryRouter } from 'react-router-dom';
import { ScrapeProvider } from '../../src/context/ScrapeContext';

for (const key of Object.keys(welfareApi)) {
    if (typeof welfareApi[key] === 'function') welfareApi[key] = async () => { throw Error('격리 테스트: 지원하지 않는 호출'); };
}
const note = '내용이 길어 확보한 32,450자 중 5,760자를 골라 AI에 전달했습니다. 빠진 부분이 있으니 대상·신청 기간·제외 조건을 원문에서 확인해주세요.';
const candidates = [{id: 1, title: '한부모 가족 교육비 지원 안내 · 긴 첨부파일 예시',
    org: '테스트 기관', link: 'https://example.invalid/post', status: '진행 중',
    input_review_required: true, input_note: note, attachment_review_required: true,
    attachment_note: '안내문.pdf: 전체 100쪽 중 앞 80쪽까지만 읽음', support_preview: '모의 분석 결과입니다.'}];
welfareApi.getNotionCandidates = async () => ({configured: true, count: 1, items: candidates});
welfareApi.getItemDetail = async () => ({...candidates[0], support_detail: '모의 지원 내용',
    ai_data: {source_input: {status: 'partial', note}, source_attachments: {
        status: 'review_required', note: candidates[0].attachment_note}}});
welfareApi.getSimilarItems = async () => [];
welfareApi.getStatus = async () => ({is_running: false});
welfareApi.getAiStatus = async () => ({busy: false});
welfareApi.getConfig = async () => ({is_configured: true, TARGET_REGION: '시험 지역', TARGET_AGE_MAX_MONTHS: 108});
welfareApi.getSites = async () => [{id: 1}];
welfareApi.getAiBacklogStatus = async () => ({backlog_total: 0});
welfareApi.getNewsProfileResults = async () => ({items: [{record_id: 'fixture', state: 'complete',
    target_status: 'match', title: candidates[0].title, revision: 1, input_note: note}], has_more: false});

createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient()}>
    <MemoryRouter><ScrapeProvider><main className="mx-auto max-w-5xl p-6 space-y-6">
        <p>격리 UI 점검 · 실제 저장·수집·AI 호출 없음</p>
        <NotionUpload />
        <NewsProfileResults profileId="fixture" onClose={() => {}} />
    </main></ScrapeProvider></MemoryRouter>
</QueryClientProvider>);

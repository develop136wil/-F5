import client from './client';
import { downloadBlob, todayStr } from '../utils/download';
import { requireDraftSourceConfirmation } from '../utils/policyDraftSourceReview';

// blob 응답 엔드포인트에서 에러 발생 시, Blob으로 변환된 JSON 에러 메시지를 텍스트로 복원
const parseBlobError = async (error) => {
    if (error?.response?.data instanceof Blob) {
        try {
            const text = await error.response.data.text();
            const json = JSON.parse(text);
            error.response.data = json; // getErrorMessage()가 읽을 수 있도록 복원
        } catch {
            // JSON 파싱 실패 시 원본 에러 유지
        }
    }
    throw error;
};

export const welfareApi = {
    getNewsRecordRetention: async () => (await client.get('/news/retention', {retry:false})).data,
    saveNewsRecordRetention: async days => (await client.post('/news/retention', {days,confirmed:true}, {retry:false,timeout:180000})).data,
    getNewsFeed: async ({profileId = 'default-news', view = 'ready', offset = 0, signal} = {}) =>
        (await client.get('/news/feed', {params: {profile_id: profileId, view, limit: 10, offset}, signal})).data,
    prepareNewsFeedAction: async (profileId, action, items) =>
        (await client.post('/news/feed/prepare', {profile_id: profileId, action, items, confirmed: true})).data,
    saveQuotaObservation: async ({confirmed,models}) => (await client.post('/ai/quota-observation', {confirmed:confirmed===true,models})).data,
    getDefaultNewsCondition: async () => (await client.get('/news/default-condition')).data,
    saveDefaultNewsCondition: async (settings, expected_revision) => (await client.put('/news/default-condition', {settings, expected_revision})).data,
    getNewsProfileTaxonomy: async () => (await client.get('/news/profile-taxonomy')).data,
    previewBokjiroSample: async (settings, maxPages = 3, limit = 20) =>
        (await client.post('/news/bokjiro-sample', { settings, max_pages: maxPages, limit })).data,
    getNewsProfiles: async signal => (await client.get('/news/profiles', {signal})).data,
    createNewsProfile: async settings => (await client.post('/news/profiles', settings)).data,
    getNewsProfileResults: async (id, offset = 0, runId) => (await client.get(`/news/profiles/${encodeURIComponent(id)}/results`, { params: { limit: 10, offset, ...(runId ? {run_id:runId} : {}) } })).data,
    getNewsResultDetails: async (id, recordId, {signal} = {}) => (await client.get(`/news/profiles/${encodeURIComponent(id)}/result-details`, {params:{record_id:recordId}, signal})).data,
    previewNewsResultImport: async (id, jobId) => (await client.get(`/news/profiles/${encodeURIComponent(id)}/jobs/${encodeURIComponent(jobId)}/import-preview`)).data,
    importNewsResult: async (id, jobId, fingerprint) => (await client.post(`/news/profiles/${encodeURIComponent(id)}/jobs/${encodeURIComponent(jobId)}/import`, {fingerprint, confirmed: true})).data,
    getNewsProfile: async id => (await client.get(`/news/profiles/${encodeURIComponent(id)}`)).data,
    saveNewsProfile: async (id, expected_revision, settings) => (await client.put(`/news/profiles/${encodeURIComponent(id)}`, { expected_revision, settings })).data,
    previewNewsTarget: async (settings, facts) => (await client.post('/news/target-preview', { settings, facts })).data,
    captureNewsDefaults: async () => (await client.post('/news/profiles/legacy/capture')).data,
    cloneNewsProfile: async (id, name) => (await client.post(`/news/profiles/${encodeURIComponent(id)}/clone`, { name })).data,
    getNewsWorkflowOptions: async id => (await client.get(`/news/profiles/${encodeURIComponent(id)}/workflow-options`)).data,
    getNewsWorkflows: async (id, signal) => (await client.get(`/news/profiles/${encodeURIComponent(id)}/workflows`, {signal})).data,
    clearNewsWorkflowHistory: async (id, workflowIds) => (await client.post(`/news/profiles/${encodeURIComponent(id)}/workflows/clear-history`, {confirmed:true,...(workflowIds ? {workflow_ids:workflowIds} : {})}, {retry:false})).data,
    startNewsWorkflow: async (id, expected_revision, {newCollection = false} = {}) => (await client.post(`/news/profiles/${encodeURIComponent(id)}/workflows`, { expected_revision, headless: true, ...(newCollection === true ? {new_collection: true} : {}) })).data,
    getNewsWorkflow: async (id, signal) => (await client.get(`/news/workflows/${encodeURIComponent(id)}`, { signal })).data,
    cancelNewsWorkflow: async id => (await client.post(`/news/workflows/${encodeURIComponent(id)}/cancel`)).data,
    resumeNewsWorkflow: async id => (await client.post(`/news/workflows/${encodeURIComponent(id)}/resume`)).data,

    getAiStatus: async () => {
        const { data } = await client.get('/ai/status');
        return data;
    },


    // Search (with pagination)
    searchItems: async (q, category, status, limit = 20, offset = 0, activeOnly = false, signal) => {
        const { data } = await client.get('/search', {
            params: { q, category, status, limit, offset, active_only: activeOnly },
            signal,
        });
        return data;
    },

    // Semantic (embedding) search
    semanticSearch: async (q, { category, limit = 20, minScore = 0.55, activeOnly = false, includeArchived = false, signal } = {}) => {
        const { data } = await client.get('/search/semantic', {
            params: { q, category, limit, min_score: minScore, active_only: activeOnly, include_archived: includeArchived },
            timeout: 30000,
            signal,
        });
        return data;
    },

    // Embedding backfill
    getEmbeddingsStatus: async () => {
        const { data } = await client.get('/embeddings/status');
        return data;
    },
    backfillEmbeddings: async (batch = 100, cursor = 0) => {
        const { data } = await client.post('/embeddings/backfill', null, { params: { batch, cursor }, timeout: 120000 });
        return data;
    },

    // AI 분석 백로그 (2026-08-06): 하루 상한 초과 등으로 아직 AI 구조화가 안 된 대기 건수
    getAiBacklogStatus: async () => {
        const { data } = await client.get('/ai-backlog/status');
        return data;
    },

    // In-app scheduling
    getSchedule: async () => {
        const { data } = await client.get('/schedule');
        return data;
    },
    saveSchedule: async (config) => {
        const { data } = await client.post('/schedule', config);
        return data;
    },

    // Windows OS-level scheduled task (앱 종료 중에도 동작)
    getOsTask: async () => {
        const { data } = await client.get('/schedule/os-task');
        return data;
    },
    registerOsTask: async (config) => {
        const { data } = await client.post('/schedule/os-task', config);
        return data;
    },
    deleteOsTask: async () => {
        const { data } = await client.delete('/schedule/os-task');
        return data;
    },

    archiveExpired: async () => {
        const { data } = await client.post('/data/archive-expired');
        return data;
    },

    getExpiringItems: async (days = 7) => {
        const { data } = await client.get('/data/expiring', { params: { days } });
        return data;
    },

    // 최근 변경된 항목 (정책 변화 등 — Notion 수정 필요 검토)
    getChangedItems: async (days = 14) => {
        const { data } = await client.get('/data/changed', { params: { days } });
        return data;
    },

    // 정책 Watch (정해진 정책 페이지 변경점 집중 감지)
    getPolicyWatch: async () => {
        const { data } = await client.get('/policy-watch');
        return data;
    },
    addPolicyWatch: async (item) => {
        const { data } = await client.post('/policy-watch', item);
        return data;
    },
    deletePolicyWatch: async (id) => {
        const { data } = await client.delete(`/policy-watch/${id}`);
        return data;
    },
    togglePolicyWatch: async (id, value) => {
        const { data } = await client.post(`/policy-watch/${id}/enabled`, null, { params: { value } });
        return data;
    },
    getPolicyWatchDiff: async (id) => {
        const { data } = await client.get(`/policy-watch/${id}/diff`);
        return data;
    },
    runPolicyWatchCheck: async () => {
        const { data } = await client.post('/policy-watch/check', null, { timeout: 180000 });
        return data;
    },

    // Notion 업로드 (단발성 행사)
    getNotionCandidates: async () => {
        const { data } = await client.get('/notion/candidates');
        return data;
    },
    verifyNotion: async () => {
        const { data } = await client.get('/notion/verify', { timeout: 90000, retry: false });
        return data;
    },
    verifyPolicyDbs: async () => {
        const { data } = await client.get('/policy/verify-dbs', { timeout: 90000, retry: false });
        return data;
    },
    allowNotionUploadRetry: async (ids) => {
        const { data } = await client.post('/notion/upload/allow-retry', { ids });
        return data;
    },
    uploadToNotion: async (ids) => {
        const { data } = await client.post('/notion/upload', { ids }, { timeout: 180000 });
        return data;
    },
    exportNewsGallery: async (ids) => {
        const response = await client.post('/news/gallery-html', { ids }, {
            responseType: 'blob',
            timeout: 180000,
        }).catch(parseBlobError);
        downloadBlob(response.data, `WelfareF5_News_${todayStr()}.html`, 'text/html');
        return {
            aiCalls: Number(response.headers?.['x-welfaref5-ai-calls'] || 0),
            cacheHits: Number(response.headers?.['x-welfaref5-poster-cache-hits'] || 0),
            posters: Number(response.headers?.['x-welfaref5-posters'] || 0),
        };
    },
    dismissNotionCandidates: async (ids) => {
        const { data } = await client.post('/notion/dismiss', { ids });
        return data;
    },

    // 정책 최신화 (연결된 정책 보관함 변경 감지)
    // (B1) "한 번에 점검" — 동기화→점검 통합 액션
    getPolicyFullCheckPlan: async (dbKeys = []) => {
        const { data } = await client.get('/policy/full-check-plan', { params: { db_keys: dbKeys.join(',') }, retry: false });
        return data;
    },
    runPolicyFullCheck: async (dbKeys = []) => {
        const { data } = await client.post('/policy/full-check', { db_keys: dbKeys });
        return data;
    },
    getPolicyFullCheckStatus: async (signal) => {
        const { data } = await client.get('/policy/full-check-status', { signal });
        return data;
    },
    syncPolicy: async (dbKeys = []) => {
        const { data } = await client.post('/policy/sync', { db_keys: dbKeys });
        return data;
    },
    getPolicySyncStatus: async (signal) => {
        const { data } = await client.get('/policy/sync-status', { signal });
        return data;
    },
    checkPolicy: async (dbKeys = []) => {
        const { data } = await client.post('/policy/check', { db_keys: dbKeys });
        return data;
    },
    getPolicyStatus: async (signal) => {
        const { data } = await client.get('/policy/status', { signal });
        return data;
    },
    auditPolicyBaseline: async (dbKeys = []) => {
        const { data } = await client.post('/policy/audit-baseline', { db_keys: dbKeys });
        return data;
    },
    getPolicyAuditStatus: async (signal) => {
        const { data } = await client.get('/policy/audit-status', { signal });
        return data;
    },
    recomputePolicyDiffs: async (dbKeys = []) => {
        const { data } = await client.post('/policy/recompute', { db_keys: dbKeys });
        return data;
    },
    getPolicyRecomputeStatus: async (signal) => {
        const { data } = await client.get('/policy/recompute-status', { signal });
        return data;
    },
    getPolicyChanges: async (dbKeys = []) => {
        const { data } = await client.get('/policy/changes', { params: { db_keys: dbKeys.join(',') } });
        return data;
    },
    getPolicyCandidates: async (dbKeys = []) => {
        const { data } = await client.get('/policy/candidates', { params: { db_keys: dbKeys.join(',') } });
        return data;
    },
    getPolicyHealth: async (dbKeys = []) => {
        const { data } = await client.get('/policy/health', { params: { db_keys: dbKeys.join(',') } });
        return data;
    },
    getPolicyTaskStatuses: async (signal) => {
        const { data } = await client.get('/policy/task-status', { signal, retry: false });
        return data;
    },
    getPolicyReviews: async (dbKeys = [], offset = 0, signal, excludePolicyIds = []) => {
        const { data } = await client.get('/policy/reviews', {
            params: { db_keys: dbKeys.join(','), limit: 50, offset, ...(excludePolicyIds.length ? {exclude_policy_ids: excludePolicyIds.join(',')} : {}) }, signal, retry: false,
        });
        return data;
    },
    getPolicyDbs: async () => {
        const { data } = await client.get('/policy/dbs');
        return data;
    },
    getDisconnectedPolicyData: async () => {
        const { data } = await client.get('/policy/disconnected-data');
        return data;
    },
    cleanupDisconnectedPolicyData: async () => {
        const { data } = await client.post('/policy/disconnected-data/cleanup');
        return data;
    },
    getPolicyWatchSchedule: async () => {
        const { data } = await client.get('/policy/watch-schedule');
        return data;
    },
    savePolicyWatchSchedule: async (cfg) => {
        const { data } = await client.post('/policy/watch-schedule', cfg);
        return data;
    },
    generatePolicyStyleGuide: async (dbKeys = []) => {
        const { data } = await client.post('/policy/style-guide/generate', { db_keys: dbKeys }, { timeout: 60000 });
        return data;
    },
    restylePolicyChanges: async (dbKeys = []) => {
        const { data } = await client.post('/policy/restyle', { db_keys: dbKeys });
        return data;
    },
    getPolicyRestyleStatus: async (signal) => {
        const { data } = await client.get('/policy/restyle-status', { signal });
        return data;
    },
    confirmPolicyChange: async (id) => {
        const { data } = await client.post('/policy/confirm', { id });
        return data;
    },
    dismissPolicyChange: async (id) => {
        const { data } = await client.post('/policy/dismiss', { id });
        return data;
    },
    applyPolicyField: async (id, field, force = false, forceSemantic = false) => {
        // (2026-08-07) force(범위축소/팽창 우회)와 forceSemantic(의미검증 우회)은 서로
        // 무관한 안전장치라 별도 플래그 — 2차 GPT Codex 리뷰로 분리.
        const { data } = await client.post('/policy/apply-field',
            { id, field, force, force_semantic: forceSemantic });
        return data;
    },
    undoPolicyField: async (id, field, receipt) => {
        const { data } = await client.post('/policy/undo-field', { id, field, receipt });
        return data;
    },
    applyPolicyFieldBulk: async (items) => {
        const { data } = await client.post('/policy/apply-field-bulk', { items }, { timeout: 120000 });
        return data;
    },
    confirmPolicyChangesBulk: async (ids) => {
        const { data } = await client.post('/policy/confirm-bulk', { ids });
        return data;
    },
    dismissPolicyChangesBulk: async (ids) => {
        const { data } = await client.post('/policy/dismiss-bulk', { ids });
        return data;
    },
    draftPolicy: async (itemId, dbKey) => {
        const { data } = await client.post('/policy/draft', { item_id: itemId, db_key: dbKey }, { timeout: 60000 });
        return data;
    },
    previewPolicyDraftFromUrl: async (url, dbKey, title = '', privacyReviewed = false) => {
        const { data } = await client.post('/policy/draft-from-url/preview',
            // 무료 API가 429 뒤 안전하게 재개될 때 HTTP 요청만 먼저 끊겨
            // 화면은 실패인데 서버 분석은 계속되는 엇갈림을 막는다.
            { url, db_key: dbKey, title: title || null, privacy_reviewed: privacyReviewed === true }, { timeout: 300000 });
        return data;
    },
    createPolicyDraftFromUrl: async (url, dbKey, fields, sourceReviewConfirmed = false) => {
        requireDraftSourceConfirmation(sourceReviewConfirmed);
        const { data } = await client.post('/policy/draft-from-url/create',
            { url, db_key: dbKey, fields, source_review_confirmed: sourceReviewConfirmed === true }, { timeout: 60000 });
        return data;
    },
    checkPolicyDraftStatus: async (url) => {
        const {data} = await client.post('/policy/draft-from-url/status', {url}, {timeout: 180000});
        return data;
    },
    allowPolicyDraftRetry: async (url, attemptId) => {
        const {data} = await client.post('/policy/draft-from-url/allow-retry',
            {url, attempt_id: attemptId, confirmed: true}, {timeout: 180000});
        return data;
    },

    // Sites
    getSites: async () => {
        const { data } = await client.get('/sites');
        return data;
    },

    // Reports
    getSummaryStats: async () => {
        const { data } = await client.get('/reports/summary');
        return data;
    },

    exportReport: async (format, filters = {}) => {
        const response = await client.get('/reports/export', {
            params: { format, ...filters },
            responseType: 'blob',
            timeout: 30000,
        }).catch(parseBlobError);
        return response.data;
    },

    // Admin / System
    analyzeKeywords: async () => {
        const { data } = await client.get('/keywords/analyze');
        return data;
    },
    getHealth: async () => {
        const { data } = await client.get('/health');
        return data;
    },
    cleanupData: async (days = 90) => {
        const { data } = await client.post('/data/cleanup', null, { params: { days } });
        return data;
    },
    generateNotionReport: async (filters = {}) => {
        const response = await client.get('/reports/notion', {
            params: { ...filters },
            responseType: 'blob',
            timeout: 30000,
        }).catch(parseBlobError);
        downloadBlob(response.data, `NotionReport_${todayStr()}.html`, 'text/html');
    },

    addSitesBatch: async (urls) => {
        const { data } = await client.post('/sites/batch', { urls });
        return data;
    },

    // 분석 없이 URL만으로 빠른 등록 (링크 추출 fallback 모드)
    addSitesQuick: async (urls) => {
        const sites = urls.map(url => {
            let hostname = url;
            let baseUrl = '';
            try {
                const u = new URL(url);
                hostname = u.hostname;
                baseUrl = u.origin;
            } catch (e) {}
            return { name: hostname, url, base_url: baseUrl, selectors: {} };
        });
        const { data } = await client.post('/sites', sites);
        return data;
    },

    getBatchStatus: async () => {
        const { data } = await client.get('/sites/batch/status');
        return data;
    },

    cancelBatchAnalysis: async () => {
        const { data } = await client.post('/sites/batch/cancel');
        return data;
    },

    deleteSite: async (siteId) => {
        const { data } = await client.delete(`/sites/${siteId}`);
        return data;
    },

    importSites: async (file) => {
        const formData = new FormData();
        formData.append('file', file);
        const { data } = await client.post('/sites/import', formData, {
            headers: { 'Content-Type': 'multipart/form-data' },
            timeout: 30000
        });
        return data;
    },

    downloadSiteTemplate: async (format = 'excel') => {
        const response = await client.get(`/sites/template/download?format=${format}`, { responseType: 'blob' }).catch(parseBlobError);
        downloadBlob(response.data, `site_import_template.${format === 'csv' ? 'csv' : 'xlsx'}`);
    },

    // Trends
    getTrends: async (days = 30) => {
        const { data } = await client.get('/reports/trends', { params: { days } });
        return data;
    },

    // Config
    getConfig: async () => {
        const { data } = await client.get('/config');
        return data;
    },

    saveConfig: async (config) => {
        const { data } = await client.post('/config', { items: config });
        return data;
    },
    verifyGoogleApiKey: async () => {
        const { data } = await client.get('/config/google/verify', { timeout: 20000 });
        return data;
    },

    // Logs
    getLogs: async () => {
        const { data } = await client.get('/logs');
        return data.logs;
    },

    // Keywords
    getKeywords: async () => {
        const { data } = await client.get('/keywords');
        return data;
    },

    addKeyword: async (keyword) => {
        const { data } = await client.post('/keywords', { keyword });
        return data;
    },

    deleteKeyword: async (keyword) => {
        const { data } = await client.delete(`/keywords/${encodeURIComponent(keyword)}`);
        return data;
    },

    // Excluded Keywords
    getExcludedKeywords: async () => {
        const { data } = await client.get('/keywords/excluded');
        return data;
    },

    addExcludedKeyword: async (keyword) => {
        const { data } = await client.post('/keywords/excluded', { keyword });
        return data;
    },

    deleteExcludedKeyword: async (keyword) => {
        const { data } = await client.delete(`/keywords/excluded/${encodeURIComponent(keyword)}`);
        return data;
    },

    // Recent activity
    getRecentItems: async (limit = 10) => {
        const { data } = await client.get('/data/recent', { params: { limit } });
        return data;
    },

    // Scraper control
    startScrape: async () => {
        const { data } = await client.post('/scrape', { headless: true });
        return data;
    },
    stopScrape: async () => {
        const { data } = await client.post('/scrape/stop');
        return data;
    },
    getStatus: async (signal) => {
        const { data } = await client.get('/status', { signal });
        return data;
    },

    // Item detail
    getItemDetail: async (itemId) => {
        const { data } = await client.get(`/data/${itemId}`);
        return data;
    },

    // Similar items (FTS5 BM25)
    getSimilarItems: async (itemId, limit = 5) => {
        const { data } = await client.get(`/data/${itemId}/similar`, { params: { limit } });
        return data;
    },

    // 변경 내역 스냅샷
    getItemSnapshots: async (itemId) => {
        const { data } = await client.get(`/data/${itemId}/snapshots`);
        return data;
    },
    getItemDiff: async (itemId, snapshotId) => {
        const { data } = await client.get(`/data/${itemId}/diff/${snapshotId}`);
        return data;
    },

    // 사이트 건강 대시보드
    getSitesHealth: async () => {
        const { data } = await client.get('/sites/health');
        return data;
    },
    getSiteHealth: async (siteId, limit = 30) => {
        const { data } = await client.get(`/sites/${siteId}/health`, { params: { limit } });
        return data;
    },

    // Notion Markdown report
    generateNotionMarkdown: async (filters = {}) => {
        const response = await client.get('/reports/notion-md', {
            params: { ...filters },
            responseType: 'blob',
            timeout: 30000,
        }).catch(parseBlobError);
        downloadBlob(response.data, `NotionReport_${todayStr()}.md`, 'text/markdown');
    },

    // DB Backup/Restore
    backupDb: async () => {
        const response = await client.get('/db/backup', { responseType: 'blob', timeout: 30000 }).catch(parseBlobError);
        downloadBlob(response.data, `welfare_backup_${todayStr()}.db`, 'application/x-sqlite3');
    },
    restoreDb: async (file) => {
        const formData = new FormData();
        formData.append('file', file);
        const { data } = await client.post('/db/restore', formData, {
            headers: { 'Content-Type': 'multipart/form-data' },
            timeout: 30000
        });
        return data;
    },

    // Log Management
    getLogFiles: async () => {
        const { data } = await client.get('/logs/files');
        return data;
    },
    downloadLog: async (filename) => {
        const response = await client.get(`/logs/download/${encodeURIComponent(filename)}`, { responseType: 'blob' });
        downloadBlob(response.data, filename);
    },
    deleteLog: async (filename) => {
        const { data } = await client.delete(`/logs/${encodeURIComponent(filename)}`);
        return data;
    },

    // Site Test
    testSite: async (siteId) => {
        const { data } = await client.post(`/sites/${siteId}/test`);
        return data;
    },
    getSiteTestStatus: async (siteId, signal) => {
        const { data } = await client.get(`/sites/${siteId}/test-status`, { signal, retry: false });
        return data;
    },
};

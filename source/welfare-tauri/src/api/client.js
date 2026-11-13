import axios from 'axios';

// ============================================================
// Tauri-native HTTP adapter
// In Tauri v2 production, the app is served under https://tauri.localhost
// Making HTTP requests from an HTTPS context is "mixed content" and gets ERR_NETWORK.
// We fix this by routing ALL requests through Tauri's native Rust HTTP stack via
// @tauri-apps/plugin-http, which bypasses WebView2 restrictions entirely.
// ============================================================

// 기본 EXE는 내장 백엔드(8421)를 사용한다. 신규 설치·회귀 테스트에서는
// VITE_API_BASE_URL로 완전히 분리된 임시 백엔드를 지정할 수 있다.
const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8421/api';
const TIMEOUT_MS = 15000;

// Determine if we're running inside Tauri (production or dev)
const isTauri = typeof window !== 'undefined' && window.__TAURI__ !== undefined;

// Build the axios adapter lazily so it works in both CJS and ESM
let _tauriFetch = null;
const getTauriFetch = async () => {
    if (_tauriFetch) return _tauriFetch;
    try {
        const mod = await import('@tauri-apps/plugin-http');
        _tauriFetch = mod.fetch;
    } catch {
        _tauriFetch = window.fetch.bind(window);
    }
    return _tauriFetch;
};

/**
 * Custom axios adapter that uses Tauri's native HTTP client.
 * This is a drop-in replacement for the default XHR adapter.
 */
const tauriAdapter = async (config) => {
    const fetchFn = await getTauriFetch();

    let url = config.url.startsWith('http')
        ? config.url
        : (config.baseURL || '').replace(/\/$/, '') + '/' + config.url.replace(/^\//, '');

    // axios의 `params` 옵션은 기본 XHR 어댑터가 자동으로 쿼리스트링에 붙여주지만,
    // 이 커스텀 어댑터는 그 처리를 대신 해주지 않아 params가 통째로 유실되고 있었다
    // (패키징된 앱에서만 재현 — 개발 모드는 기본 어댑터를 쓰므로 못 잡았음).
    if (config.params && typeof config.params === 'object') {
        const qs = new URLSearchParams();
        for (const [key, value] of Object.entries(config.params)) {
            if (value === undefined || value === null) continue;
            qs.append(key, value);
        }
        const qsStr = qs.toString();
        if (qsStr) {
            url += (url.includes('?') ? '&' : '?') + qsStr;
        }
    }

    const method = (config.method || 'GET').toUpperCase();
    const headers = { ...(config.headers || {}) };
    // Remove axios internal headers that may confuse the native client
    delete headers['common'];
    delete headers['delete'];
    delete headers['get'];
    delete headers['head'];
    delete headers['post'];
    delete headers['put'];
    delete headers['patch'];

    // (2026-08-07, GPT Codex 전수조사로 발견) FormData를 JSON.stringify()하면 "{}"가 돼버려
    // DB 복원·사이트 가져오기 같은 파일 업로드가 패키징된 앱에서만 조용히 깨졌다(개발
    // 브라우저는 기본 axios 어댑터를 써서 이 커스텀 어댑터를 안 거치므로 못 잡았음).
    // FormData는 그대로 body로 넘기고, fetch가 boundary 포함 Content-Type을 직접 설정하게
    // 호출자가 수동으로 넣은 'multipart/form-data' 헤더(boundary 없음)는 제거해야 한다.
    let body = undefined;
    if (config.data instanceof FormData) {
        body = config.data;
        delete headers['Content-Type'];
        delete headers['content-type'];
    } else if (config.data !== undefined && config.data !== null) {
        body = typeof config.data === 'string'
            ? config.data
            : JSON.stringify(config.data);
    }

    const controller = new AbortController();
    let timedOut = false;
    const cancel = () => controller.abort();
    const aborted = new Promise((_, reject) => {
        controller.signal.addEventListener('abort', () => {
            const error = new Error('요청이 취소되었습니다.');
            error.name = 'AbortError';
            reject(error);
        }, { once: true });
    });
    const waitFor = promise => Promise.race([promise, aborted]);
    config.signal?.addEventListener('abort', cancel, { once: true });
    const timeoutId = config.timeout
        ? setTimeout(() => { timedOut = true; controller.abort(); }, config.timeout)
        : null;

    try {
        if (config.signal?.aborted) {
            const error = new Error('요청이 취소되었습니다.');
            error.name = 'AbortError';
            throw error;
        }
        const response = await waitFor(fetchFn(url, {
            method,
            headers,
            body,
            signal: controller.signal,
        }));

        let data;
        // (2026-08-07, GPT Codex 전수조사로 발견) responseType:'blob'을 무시하고 항상
        // json()/text()로 읽어서, DB 백업·Excel 리포트·사이트 템플릿 다운로드가 패키징된
        // 앱에서 바이너리를 텍스트로 깨뜨렸다(UTF-8 재인코딩으로 손상). welfare.js의
        // parseBlobError()도 response.data가 Blob 인스턴스임을 전제로 하므로, 에러
        // 상태에서도 동일하게 blob()으로 읽어야 그 에러 파싱 경로도 정상 동작한다.
        if (config.responseType === 'blob') {
            data = await waitFor(response.blob());
        } else {
            const ct = response.headers?.get?.('content-type') || '';
            data = await waitFor(ct.includes('application/json')
                ? response.json() : response.text());
        }

        if (!response.ok) {
            const err = new Error(`Request failed with status ${response.status}`);
            err.response = { data, status: response.status, headers: {}, config };
            err.status = response.status;
            throw err;
        }

        return { data, status: response.status, statusText: 'OK', headers: {}, config };
    } catch (err) {
        if (timeoutId) clearTimeout(timeoutId);
        if (err.name === 'AbortError') {
            const timeoutErr = new Error('timeout of ' + config.timeout + 'ms exceeded');
            timeoutErr.code = timedOut ? 'ECONNABORTED' : 'ERR_CANCELED';
            if (!timedOut) timeoutErr.message = '요청이 취소되었습니다.';
            throw timeoutErr;
        }
        throw err;
    } finally {
        if (timeoutId) clearTimeout(timeoutId);
        config.signal?.removeEventListener('abort', cancel);
    }
};

// Create the axios instance — use Tauri adapter in production, XHR in browser dev
const client = axios.create({
    baseURL: BASE_URL,
    timeout: TIMEOUT_MS,
    headers: { 'Content-Type': 'application/json' },
    ...(isTauri ? { adapter: tauriAdapter } : {}),
});

// AI endpoint timeout (longer for Gemini calls)
let desktopConnection;
client.interceptors.request.use(async (config) => {
    if (isTauri) {
        if (!desktopConnection) {
            desktopConnection = import('@tauri-apps/api/core')
                .then(({ invoke }) => invoke('desktop_connection'))
                .catch((error) => { desktopConnection = null; throw error; });
        }
        const connection = await desktopConnection;
        // Desktop must never follow a build-time URL to an unrelated backend.
        config.baseURL = connection.base_url;
        config.headers['X-Welfare-Session'] = connection.token;
    }
    if (config.url?.includes('/ai/') || config.url?.includes('/analyze')) {
        config.timeout = 120000;
    }
    return config;
});

// Retry on network error (for slow sidecar startup) — only in non-Tauri mode
// In Tauri mode the adapter handles errors directly
if (!isTauri) {
    const delay = (ms) => new Promise((res) => setTimeout(res, ms));
    client.interceptors.response.use(
        (res) => res,
        async (error) => {
            const config = error.config;
            const isNetworkError = !error.response || error.code === 'ECONNABORTED';
            const safeToRetry = config && config.retry !== false && ['get', 'head'].includes((config.method || 'get').toLowerCase()) && error.code !== 'ERR_CANCELED';
            if (safeToRetry && isNetworkError && (!config._retryCount || config._retryCount < 3)) {
                config._retryCount = (config._retryCount || 0) + 1;
                await delay(500);
                return client(config);
            }
            return Promise.reject(error);
        }
    );
}

export default client;

import { useState, useEffect } from 'react';
import { Loader2, WifiOff, RefreshCw } from 'lucide-react';
import client from '../api/client';

/**
 * ConnectionGuard wraps the entire app and blocks rendering
 * until the backend API server (sidecar) is ready.
 * Shows a loading screen during startup and an error screen on failure.
 */
export default function ConnectionGuard({ children }) {
    const [status, setStatus] = useState('connecting'); // 'connecting' | 'connected' | 'failed'
    const [attempt, setAttempt] = useState(0);
    const [lastError, setLastError] = useState('');
    const [retry, setRetry] = useState(0);
    const [elapsed, setElapsed] = useState(0);

    const MAX_ATTEMPTS = 60;
    const INTERVAL_MS = 2000;

    useEffect(() => {
        if (status !== 'connecting') return;
        const started = Date.now();
        setElapsed(0);
        const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
        return () => clearInterval(timer);
    }, [status, retry]);

    useEffect(() => {
        let cancelled = false;
        let timer = null;

        const checkHealth = async () => {
            try {
                await client.get('/ready');
                if (!cancelled) setStatus('connected');
            } catch (err) {
                const errMsg = err?.code || err?.message || String(err || 'Unknown');
                if (!cancelled) {
                    setLastError(errMsg);
                    setAttempt(prev => {
                        const next = prev + 1;
                        if (next >= MAX_ATTEMPTS) {
                            setStatus('failed');
                            return next;
                        }
                        timer = setTimeout(checkHealth, INTERVAL_MS);
                        return next;
                    });
                }
            }
        };

        checkHealth();

        return () => {
            cancelled = true;
            if (timer) clearTimeout(timer);
        };
    }, [retry]);

    const handleRetry = () => {
        setStatus('connecting');
        setAttempt(0);
        setRetry(n => n + 1);
    };

    if (status === 'connected') {
        return children;
    }

    if (status === 'failed') {
        return (
            <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
                <div className="max-w-sm w-full text-center space-y-6 p-8">
                    <div className="mx-auto w-16 h-16 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center">
                        <WifiOff className="w-8 h-8 text-red-500" />
                    </div>
                    <div>
                        <h2 className="text-xl font-bold text-gray-800 dark:text-gray-100 mb-2">
                            서버 연결 실패
                        </h2>
                        <p className="text-sm text-gray-500 dark:text-gray-400">
                            백엔드 서버에 연결할 수 없습니다 ({attempt}/{MAX_ATTEMPTS} 시도 실패). 앱을 다시 시작하거나 아래 버튼을 눌러 재시도 해주세요.
                        </p>
                        {lastError && (
                            <p className="mt-2 text-xs font-mono bg-gray-100 dark:bg-gray-800 text-red-500 rounded px-2 py-1 break-all">
                                오류 코드: {lastError}
                            </p>
                        )}
                    </div>
                    <button
                        onClick={handleRetry}
                        className="inline-flex items-center gap-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors font-medium"
                    >
                        <RefreshCw className="w-4 h-4" />
                        재연결
                    </button>
                </div>
            </div>
        );
    }

    // Connecting state — loading screen
    return (
        <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
            <div className="text-center space-y-4">
                <Loader2 className="w-10 h-10 text-blue-500 animate-spin mx-auto" />
                <div>
                    <h2 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
                        서버 시작 중...
                    </h2>
                    <p className="text-sm text-gray-400 mt-1">
                        잠시만 기다려 주세요 ({attempt}/{MAX_ATTEMPTS}) — 약 {elapsed}초 경과
                    </p>
                    <div className="mt-3 w-48 mx-auto bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                        <div
                            className="bg-blue-500 h-1.5 rounded-full transition-all duration-500"
                            style={{ width: `${Math.min((attempt / MAX_ATTEMPTS) * 100, 100)}%` }}
                        />
                    </div>
                </div>
            </div>
        </div>
    );
}

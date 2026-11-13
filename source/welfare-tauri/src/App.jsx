
import React, { Suspense, useEffect } from 'react';
import { Navigate, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import Layout from './components/layout/Layout';
import PageTransition from './components/layout/PageTransition';
import ErrorBoundary from './components/ErrorBoundary';
import ConnectionGuard from './components/ConnectionGuard';
import { settingsDrafts } from './utils/settingsDrafts';
import { shouldHandleSearchShortcut } from './utils/searchShortcut';
import { AnimatePresence } from 'framer-motion';

const Home = React.lazy(() => import('./pages/Home'));
const SearchPage = React.lazy(() => import('./pages/Search'));
const Reports = React.lazy(() => import('./pages/Reports'));
const Sites = React.lazy(() => import('./pages/Sites'));
const Settings = React.lazy(() => import('./pages/Settings'));
const NotionUpload = React.lazy(() => import('./pages/NotionUpload'));
const PolicyManage = React.lazy(() => import('./pages/PolicyManage'));
import { Toaster } from 'sonner';

function App() {
    const location = useLocation();
    const navigate = useNavigate();
    useEffect(() => {
        const warn = event => {
            if (!settingsDrafts.dirtyNames().length && !settingsDrafts.isWriting()) return;
            event.preventDefault();
            event.returnValue = '';
        };
        window.addEventListener('beforeunload', warn);
        return () => window.removeEventListener('beforeunload', warn);
    }, []);

    // 전역 단축키: Ctrl/Cmd+K 또는 "/"로 검색창 포커스 (입력 중이 아닐 때)
    useEffect(() => {
        const focusSearch = () => {
            const el = document.getElementById('global-search-input');
            if (el) { el.focus(); el.select?.(); return true; }
            return false;
        };
        const onKeyDown = (e) => {
            const modalOpen = Boolean(document.querySelector('dialog[open], [role="dialog"][aria-modal="true"]'));
            if (shouldHandleSearchShortcut(e, modalOpen)) {
                e.preventDefault();
                if (!focusSearch()) {
                    navigate('/search');
                    // 라우트 전환(lazy) 후 포커스 재시도
                    setTimeout(focusSearch, 120);
                    setTimeout(focusSearch, 350);
                }
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [navigate]);

    // 외부 링크(target=_blank)를 시스템 기본 브라우저로 연다.
    // Tauri 웹뷰는 <a target="_blank">를 그냥 무시하므로 opener 플러그인으로 처리.
    useEffect(() => {
        const onClick = async (e) => {
            const a = e.target?.closest?.('a[target="_blank"]');
            if (!a) return;
            const href = a.getAttribute('href') || a.href;
            if (!href || !/^https?:\/\//i.test(href)) return;
            e.preventDefault();
            try {
                // opener 플러그인 명령을 core invoke로 직접 호출 (JS 플러그인 패키지 불필요 → 버전충돌 회피)
                const { invoke } = await import('@tauri-apps/api/core');
                await invoke('plugin:opener|open_url', { url: href });
            } catch (err) {
                try { window.open(href, '_blank'); } catch { /* noop */ }
            }
        };
        document.addEventListener('click', onClick);
        return () => document.removeEventListener('click', onClick);
    }, []);


    return (
        <ErrorBoundary>
            <ConnectionGuard>
                <Layout>
                    <Toaster position="top-center" richColors />
                    <AnimatePresence mode="wait">
                        <Suspense fallback={<div role="status" className="flex h-screen w-full items-center justify-center text-gray-600">화면을 불러오는 중입니다…</div>}>
                            <Routes location={location} key={location.pathname}>
                                <Route path="/" element={<PageTransition><Home /></PageTransition>} />
                                <Route path="/search" element={<PageTransition><SearchPage /></PageTransition>} />
                                <Route path="/reports" element={<PageTransition><Reports /></PageTransition>} />
                                <Route path="/sites" element={<PageTransition><Sites /></PageTransition>} />
                                <Route path="/policy-watch" element={<Navigate replace to="/policy" />} />
                                <Route path="/notion" element={<PageTransition><NotionUpload /></PageTransition>} />
                                <Route path="/policy" element={<PageTransition><PolicyManage /></PageTransition>} />
                                <Route path="/settings" element={<PageTransition><Settings /></PageTransition>} />
                                <Route path="/tutorial" element={<Navigate replace to="/settings?tab=guide" />} />
                            </Routes>
                        </Suspense>
                    </AnimatePresence>
                </Layout>
            </ConnectionGuard>
        </ErrorBoundary>
    );
}

export default App;


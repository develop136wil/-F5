import React, { createContext, useContext, useState, useEffect, useRef, useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { getErrorMessage } from '../utils/error';
import { scrapeCompletionNotice } from '../utils/scrapeCompletion';
import { isPermissionGranted, requestPermission, sendNotification } from '@tauri-apps/plugin-notification';

const ScrapeContext = createContext();

/**
 * 스크래핑 상태를 전역으로 관리한다.
 * 폴링/완료 토스트/데이터 갱신을 어느 페이지에 있든 유지하기 위해
 * Home 로컬 상태에서 끌어올렸다. 사이드바 인디케이터·대시보드가 함께 소비한다.
 */
export const ScrapeProvider = ({ children }) => {
    const queryClient = useQueryClient();
    const [isScraping, setIsScraping] = useState(false);
    const [isStarting, setIsStarting] = useState(false);
    const [isStopping, setIsStopping] = useState(false);
    const commandPendingRef = useRef(false);
    const [progress, setProgress] = useState(0);
    const [statusMessage, setStatusMessage] = useState('');
    const [aiSavings, setAiSavings] = useState(null);
    const [completionNotice, setCompletionNotice] = useState(null);

    const failCountRef = useRef(0);
    const failWarnedRef = useRef(false);

    const invalidateData = useCallback(() => {
        queryClient.invalidateQueries({ queryKey: queryKeys.summaryStats });
        queryClient.invalidateQueries({ queryKey: queryKeys.recentItems(10) });
        queryClient.invalidateQueries({ queryKey: queryKeys.trends(30) });
        // (2026-08-07, GPT Codex 리뷰) 수집 완료 때 백로그 재처리도 함께 도는데 이게 빠져
        // 있어서, 실제 DB는 147→117로 줄었어도 대시보드는 창 포커스 변경/재진입 전까지
        // 이전 수치를 계속 보여줬다.
        queryClient.invalidateQueries({ queryKey: queryKeys.aiBacklogStatus });
        queryClient.invalidateQueries({ queryKey: queryKeys.notionCandidates });
        queryClient.invalidateQueries({ queryKey: queryKeys.newsFeed });
    }, [queryClient]);

    // 유휴 중에도 예약 실행을 감지. 응답 완료 후 다음 요청을 예약해 중첩 방지.
    useEffect(() => {
        const controller = new AbortController();
        let timer;
        failCountRef.current = 0;
        failWarnedRef.current = false;

        const poll = async () => {
            if (controller.signal.aborted) return;
            try {
                const status = await welfareApi.getStatus(controller.signal);
                if (controller.signal.aborted) return;
                failCountRef.current = 0;
                failWarnedRef.current = false;
                setProgress(Math.min(100, Math.max(0, status.progress || 0)));
                setStatusMessage(status.message || '');
                setIsScraping(Boolean(status.is_running));
                const notice = scrapeCompletionNotice(status);
                setCompletionNotice(notice?.tone === 'success' ? null : notice);

                if (!status.is_running && isScraping) {
                    setIsScraping(false);
                    setAiSavings(status.ai_savings || null);
                    invalidateData();

                    if (notice?.tone === 'warning') {
                        toast.warning(notice.message);
                    } else if (notice?.tone === 'error') {
                        toast.error(notice.message);
                    } else if (notice?.tone === 'success') {
                        toast.success(notice.message);
                        try {
                            let granted = await isPermissionGranted();
                            if (!granted) granted = (await requestPermission()) === 'granted';
                            if (granted) {
                                sendNotification({
                                    title: '복지F5 수집 완료',
                                    body: '처리가 끝났습니다. 새 소식 화면에서 결과를 확인하세요.',
                                });
                            }
                        } catch (e) {
                            console.error('Failed to send notification:', e);
                        }
                    }
                }
            } catch (error) {
                if (controller.signal.aborted) return;
                console.error('Failed to fetch status:', error);
                failCountRef.current += 1;
                if (failCountRef.current >= 5 && !failWarnedRef.current) {
                    failWarnedRef.current = true;
                    toast.warning('서버 상태 확인에 연속 실패하고 있습니다. 연결 상태를 확인해주세요.');
                }
            } finally {
                if (!controller.signal.aborted) timer = setTimeout(poll, isScraping ? 2000 : 10000);
            }
        };
        poll();

        return () => {
            controller.abort();
            clearTimeout(timer);
        };
    }, [isScraping, invalidateData]);

    const startScrape = useCallback(async () => {
        if (commandPendingRef.current || isScraping) return;
        commandPendingRef.current = true;
        setIsStarting(true);
        try {
            await welfareApi.startScrape();
            setIsScraping(true);
            setProgress(0);
            setStatusMessage('준비 중...');
            setAiSavings(null);
            setCompletionNotice(null);
            toast.info('새 소식 찾기를 시작합니다.');
        } catch (error) {
            toast.error('새 소식 찾기 시작 실패: ' + getErrorMessage(error));
        } finally {
            commandPendingRef.current = false;
            setIsStarting(false);
        }
    }, [isScraping]);

    const stopScrape = useCallback(async () => {
        if (commandPendingRef.current) return;
        commandPendingRef.current = true;
        setIsStopping(true);
        try {
            await welfareApi.stopScrape();
            setStatusMessage('중지 요청됨...');
            toast.info('중지 요청을 보냈습니다...');
        } catch (error) {
            if (error?.response?.status === 409) {
                toast.warning('실행 중인 작업이 없습니다.');
            } else {
                toast.error('중지 요청 실패: ' + getErrorMessage(error));
            }
        } finally {
            commandPendingRef.current = false;
            setIsStopping(false);
        }
    }, []);

    return (
        <ScrapeContext.Provider value={{ isScraping, isStarting, isStopping, progress, statusMessage, aiSavings, completionNotice, startScrape, stopScrape }}>
            {children}
        </ScrapeContext.Provider>
    );
};

export const useScrape = () => {
    const ctx = useContext(ScrapeContext);
    if (!ctx) throw new Error('useScrape must be used within a ScrapeProvider');
    return ctx;
};

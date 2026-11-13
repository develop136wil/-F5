import { useCallback, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { welfareApi } from '../api/welfare';
import { queryKeys } from '../api/queryKeys';
import { getErrorMessage } from '../utils/error';
import { startStatusPolling } from '../utils/statusPolling';
import { siteTestOutcome, validateSiteTestTask } from '../utils/siteTestTask';

const toastId = siteId => `site-test-${siteId}`;

export function useSiteTest(sites) {
    const queryClient = useQueryClient();
    // Memory only: retain the active identity across page navigation, never
    // persist site data or start a second inspection to recover a lost response.
    const { data: task } = useQuery({
        queryKey: queryKeys.siteTestTask, queryFn: () => null, enabled: false,
        initialData: null, staleTime: Infinity, gcTime: Infinity,
    });

    useEffect(() => {
        const current = queryClient.getQueryData(queryKeys.siteTestTask);
        if (current?.running) return;
        const running = sites?.find(site => site.test_task?.running)?.test_task;
        if (!running || current?.task_id === running.task_id) return;
        try {
            queryClient.setQueryData(queryKeys.siteTestTask, validateSiteTestTask(running, running.site_id));
            toast.loading('사이트 점검 중...', { id: toastId(running.site_id) });
        } catch { /* A stale or malformed list snapshot must not start a job. */ }
    }, [sites, queryClient]);

    const siteId = task?.site_id;
    const polling = Boolean(task?.running && task.status !== 'starting');
    useEffect(() => {
        if (!polling) return;
        return startStatusPolling({
            fetchStatus: async signal => {
                try {
                    return validateSiteTestTask(await welfareApi.getSiteTestStatus(siteId, signal), siteId);
                } catch (error) {
                    // A confirmed removal is different from an intermittent
                    // connection failure; do not leave the UI locked forever.
                    if (error.response?.status === 404) return {
                        site_id: siteId, task_id: `missing-site-${siteId}`, status: 'failed', running: false,
                        message: '등록된 사이트를 찾지 못해 점검 표시를 종료했습니다.', result: null,
                    };
                    throw error;
                }
            },
            onStatus: snapshot => {
                queryClient.setQueryData(queryKeys.siteTestTask, snapshot);
                if (snapshot.running) toast.loading('사이트 점검 중...', { id: toastId(siteId) });
            },
            onError: () => {
                toast.loading('연결을 확인하고 있습니다. 점검 상태를 다시 확인합니다.', { id: toastId(siteId) });
            },
        });
    }, [siteId, polling, queryClient]);

    useEffect(() => {
        if (!task || task.running || task.notified) return;
        const outcome = siteTestOutcome(task);
        toast[outcome.level](outcome.message, { id: toastId(task.site_id), duration: 6000 });
        queryClient.setQueryData(queryKeys.siteTestTask, current => current === task ? { ...current, notified: true } : current);
        queryClient.invalidateQueries({ queryKey: queryKeys.sites });
    }, [task, queryClient]);

    const start = useCallback(async siteId => {
        // Query cache is synchronous, so two clicks before React renders cannot
        // enqueue duplicate requests (the backend also reuses a running task).
        if (queryClient.getQueryData(queryKeys.siteTestTask)?.running) return;
        queryClient.setQueryData(queryKeys.siteTestTask, { site_id: siteId, running: true, status: 'starting' });
        toast.loading('사이트 점검 중...', { id: toastId(siteId) });
        try {
            const snapshot = validateSiteTestTask(await welfareApi.testSite(siteId), siteId);
            queryClient.setQueryData(queryKeys.siteTestTask, snapshot);
        } catch (error) {
            if (error.response?.status >= 400 && error.response.status < 500) {
                queryClient.setQueryData(queryKeys.siteTestTask, null);
                toast[error.response.status === 409 ? 'info' : 'error'](error.response.status === 409
                    ? '다른 작업이 진행 중입니다. 잠시 후 다시 점검해주세요.' : '점검을 시작하지 못했습니다: ' + getErrorMessage(error),
                { id: toastId(siteId), duration: 6000 });
                queryClient.invalidateQueries({ queryKey: queryKeys.sites });
            } else {
                // The start request may have reached the server. Read its
                // state instead of retrying POST or declaring the job failed.
                queryClient.setQueryData(queryKeys.siteTestTask, { site_id: siteId, running: true, status: 'reconnecting' });
                toast.loading('점검 시작 여부를 확인하고 있습니다.', { id: toastId(siteId) });
            }
        }
    }, [queryClient]);

    return { task, start, isRunning: Boolean(task?.running) };
}

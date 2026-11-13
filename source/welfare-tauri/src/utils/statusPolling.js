const defaultIsBackground = () =>
    typeof document !== 'undefined' && document.visibilityState === 'hidden';

/**
 * 백그라운드 작업 상태를 중첩 없이 조회한다.
 *
 * setInterval(async ...)과 달리 현재 응답이 끝난 뒤에만 다음 요청을 예약한다.
 * 반환된 함수를 호출하면 예약된 조회와 진행 중 HTTP 요청을 함께 취소한다.
 */
export const startStatusPolling = ({
    fetchStatus,
    onStatus = () => {},
    onComplete = () => {},
    onError = () => {},
    intervalMs = 1500,
    backgroundIntervalMs = 5000,
    isBackground = defaultIsBackground,
    schedule = (fn, delay) => setTimeout(fn, delay),
    cancel = timer => clearTimeout(timer),
}) => {
    let stopped = false;
    let timer = null;
    let controller = null;

    const queueNext = () => {
        if (stopped) return;
        const delay = isBackground() ? backgroundIntervalMs : intervalMs;
        timer = schedule(run, delay);
    };

    const run = async () => {
        if (stopped) return;
        timer = null;
        const currentController = new AbortController();
        controller = currentController;
        try {
            const status = await fetchStatus(currentController.signal);
            if (stopped || currentController.signal.aborted) return;
            // Keep the last status on malformed successful responses too.
            if (!status || typeof status !== 'object' || Array.isArray(status) ||
                typeof status.running !== 'boolean') {
                throw new Error('작업 진행 상태를 확인하지 못했습니다. 다시 확인 중입니다.');
            }
            onStatus(status);
            if (!status.running) {
                stopped = true;
                onComplete(status);
            }
        } catch (error) {
            if (!stopped && !currentController.signal.aborted) onError(error);
        } finally {
            if (controller === currentController) controller = null;
            queueNext();
        }
    };

    queueNext();
    return () => {
        stopped = true;
        if (timer !== null) cancel(timer);
        timer = null;
        controller?.abort();
        controller = null;
    };
};

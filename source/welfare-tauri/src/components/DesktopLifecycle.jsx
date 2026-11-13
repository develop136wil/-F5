import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Loader2, ShieldCheck, AlertCircle } from 'lucide-react';
import { settingsDrafts } from '../utils/settingsDrafts';
import { useSettingsDirtyNames } from '../hooks/useSettingsDraft';
import { advanceConnection, connectionView } from '../utils/desktopConnection';

const desktop = typeof window !== 'undefined' && !!window.__TAURI__;
// A native IPC response can disappear. Bound the UI wait, not the backend job:
// timing out never cancels a save or proves that shutdown was rejected.
const command = async (name, args) => {
    let timer;
    try {
        return await Promise.race([
            import('@tauri-apps/api/core').then(({ invoke }) => invoke(name, args)),
            new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error('프로그램의 응답을 확인하지 못했습니다. 종료 여부는 아직 확인되지 않았습니다. 잠시 후 ‘종료 다시 확인’을 눌러주세요. 강제로 종료하지 않았습니다.')), 10000);
            }),
        ]);
    } finally { clearTimeout(timer); }
};

/** Native close is always intercepted until our own backend has exited. */
export default function DesktopLifecycle({ children }) {
    const dirtyNames = useSettingsDirtyNames();
    const [status, setStatus] = useState({ phase: 'starting' });
    const [mode, setMode] = useState(null);
    const [error, setError] = useState('');
    const [elapsed, setElapsed] = useState(0);
    const [hasConnected, setHasConnected] = useState(false);
    const modeRef = useRef(null);
    const dialogRef = useRef(null);
    const keepRef = useRef(null);
    const requestRef = useRef(false);
    const connectionRef = useRef(null);
    const previousFocusRef = useRef(null);
    const connectionFocusedRef = useRef(false);
    const { retainApp, blockActions, reconnecting } = connectionView(hasConnected, status);
    const changeMode = (value) => { modeRef.current = value; setMode(value); };

    const begin = async () => {
        if (requestRef.current) return;
        if (settingsDrafts.isWriting()) {
            setError('설정 저장 요청을 처리하고 있습니다. 완료된 뒤 다시 종료해주세요.');
            changeMode('error');
            return;
        }
        if (settingsDrafts.dirtyNames().length && modeRef.current !== 'confirm') {
            changeMode('confirm');
            return;
        }
        requestRef.current = true;
        changeMode('waiting');
        setElapsed(0);
        setError('');
        try { await command('desktop_shutdown'); }
        catch (err) { setError(String(err)); changeMode('error'); }
        finally { requestRef.current = false; }
    };

    useEffect(() => {
        if (!desktop) return;
        let disposed = false;
        let timer;
        let unlisten;
        const poll = async () => {
            try {
                const next = await command('desktop_status');
                if (disposed) return;
                setStatus(previous => advanceConnection(previous, next));
                if (next.phase === 'ready') setHasConnected(true);
                if (next.shutdown_requested) changeMode('waiting');
            } catch (err) {
                if (!disposed) setStatus({ phase: 'starting', message: String(err) });
            }
            if (!disposed) timer = setTimeout(poll, 1500);
        };
        const close = async (event) => {
            if (disposed) return;
            // Acknowledge before any potentially slow backend check. The native
            // fallback must not bypass the draft/busy confirmation shown here.
            try { await command('desktop_close_ack', { requestId: event.payload }); }
            catch (err) { setError(String(err)); changeMode('error'); return; }
            if (modeRef.current) return;
            setElapsed(0);
            changeMode('checking');
            try {
                const next = await command('desktop_status');
                if (disposed) return;
                setStatus(next);
                if (next.can_exit || (next.phase === 'ready' && !next.busy)) await begin();
                else if (next.phase === 'ready') changeMode('confirm');
                else {
                    setError('프로그램의 상태를 확인하고 있습니다. 잠시 후 다시 종료해주세요. 강제로 종료하지 않았습니다.');
                    changeMode('error');
                }
            } catch (err) { if (!disposed) { setError(String(err)); changeMode('error'); } }
        };
        import('@tauri-apps/api/event').then(async ({ listen }) => {
            const stop = await listen('desktop-close-requested', close);
            if (disposed) stop(); else unlisten = stop;
        });
        poll();
        return () => { disposed = true; clearTimeout(timer); unlisten?.(); };
    }, []);

    useEffect(() => {
        if (mode !== 'waiting' && mode !== 'checking') return;
        const timer = setInterval(() => setElapsed(n => n + 1), 1000);
        return () => clearInterval(timer);
    }, [mode]);

    useEffect(() => {
        if (mode) (keepRef.current || dialogRef.current)?.focus();
    }, [mode]);

    useLayoutEffect(() => {
        if (!desktop) return;
        if (mode) {
            // Restore the last app control after cancelling a close dialog too.
            connectionFocusedRef.current = true;
            return;
        }
        if (blockActions) {
            connectionRef.current?.focus({ preventScroll: true });
            connectionFocusedRef.current = true;
        } else if (connectionFocusedRef.current) {
            connectionFocusedRef.current = false;
            const target = previousFocusRef.current;
            if (target?.isConnected && !target.closest('[inert]') && !target.disabled) {
                target.focus({ preventScroll: true });
            }
        }
    }, [blockActions, mode]);

    if (!desktop) return children;
    return <>
        {retainApp && <div onFocusCapture={event => { previousFocusRef.current = event.target; }} inert={blockActions || mode ? '' : undefined} aria-hidden={blockActions || mode ? true : undefined}>{children}</div>}
        {reconnecting && !blockActions && !mode && <div role="status" aria-live="polite" className="pointer-events-none fixed bottom-4 right-4 z-[80] max-w-[calc(100vw-2rem)] rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 shadow-sm dark:border-amber-800 dark:bg-zinc-900 dark:text-amber-200">
            <p>연결 확인이 잠시 늦어지고 있어요.</p>
            <p className="mt-1 text-xs">입력은 유지됩니다. 저장 결과를 기다리는 중이라면 다시 누르지 마세요.</p>
        </div>}
        {blockActions && <div inert={mode ? '' : undefined} aria-hidden={mode ? true : undefined} className={`${retainApp ? 'fixed inset-0 z-[90] bg-zinc-100/70 backdrop-blur-md dark:bg-zinc-950/70' : 'min-h-screen bg-zinc-50 dark:bg-zinc-950'} flex items-center justify-center p-8`}>
            <section ref={connectionRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="desktop-connection-title"
                onKeyDown={event => {
                    if (event.key !== 'Tab') return;
                    event.preventDefault();
                    (event.currentTarget.querySelector('button') || event.currentTarget).focus();
                }}
                className="w-full max-w-md rounded-3xl border border-zinc-200/70 bg-white/[0.85] p-8 text-center shadow-lg shadow-zinc-900/5 outline-none backdrop-blur-xl dark:border-zinc-700/60 dark:bg-zinc-900/90 dark:shadow-black/20">
                {status.phase === 'error' ? <AlertCircle className="mx-auto mb-5 h-10 w-10 text-amber-500" /> : <Loader2 className="mx-auto mb-5 h-10 w-10 animate-spin text-blue-600" />}
                <h1 id="desktop-connection-title" className="text-2xl font-semibold leading-snug text-zinc-900 dark:text-white">{status.phase === 'error' ? '실행 상태를 확인해주세요' : status.phase === 'stopping' ? '프로그램 종료를 확인하고 있어요' : reconnecting ? '연결 상태를 확인하고 있어요' : '복지F5를 준비하고 있어요'}</h1>
                {reconnecting && <p className="mt-3 text-xs leading-5 text-zinc-500 dark:text-zinc-400" role="status">열어둔 화면은 유지됩니다. 연결이 확인되면 이어서 사용할 수 있습니다. 진행 중인 작업을 다시 시작하지 마세요.</p>}
                <p className="mt-3 text-xs leading-5 text-zinc-500 dark:text-zinc-400 break-words" role="status">{status.message || '저장된 정보를 안전하게 연결하고 있습니다. 잠시만 기다려주세요.'}</p>
                {status.can_exit && <button onClick={begin} className="mt-6 rounded-xl bg-blue-600 px-6 py-3 text-sm font-semibold text-white hover:bg-blue-700">이 창 닫기</button>}
            </section>
        </div>}
        {mode && <div className="fixed inset-0 z-[100] flex items-center justify-center bg-zinc-900/20 p-6 backdrop-blur-md dark:bg-black/40">
            <section ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="desktop-close-title"
                onKeyDown={(event) => {
                    if (event.key === 'Escape' && mode === 'confirm') changeMode(null);
                    if (event.key === 'Tab') {
                        const buttons = [...event.currentTarget.querySelectorAll('button')];
                        if (!buttons.length) { event.preventDefault(); return; }
                        const first = buttons[0], last = buttons[buttons.length - 1];
                        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
                    }
                }}
                className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-3xl border border-zinc-200/70 bg-white/[0.85] p-7 shadow-lg shadow-zinc-900/5 outline-none backdrop-blur-xl dark:border-zinc-700/60 dark:bg-zinc-900/90 dark:shadow-black/20">
                <ShieldCheck className="mb-4 h-9 w-9 text-blue-600" />
                <h2 id="desktop-close-title" className="text-2xl font-semibold leading-snug text-zinc-900 dark:text-white">{mode === 'confirm' ? '작업을 마무리하고 종료할까요?' : mode === 'error' ? '종료 상태를 확인해주세요' : '안전하게 종료하고 있어요'}</h2>
                <div className="mt-3 text-xs leading-5 text-zinc-500 dark:text-zinc-400" aria-live="polite">
                    {mode === 'confirm' ? <><p>{status.busy ? `진행 중인 작업이 있습니다${status.job ? ` (${status.job})` : ''}.` : '프로그램을 종료할까요?'} 수집·분석에는 중단을 요청하고, 저장·복원 중인 정보는 안전하게 마무리한 뒤 창을 닫습니다.</p>{dirtyNames.length > 0 && <p className="mt-3 font-semibold text-amber-700">아직 저장하지 않은 설정이 있습니다. 종료하면 미저장 입력은 사라집니다. 저장하려면 ‘창 유지하기’를 선택해주세요.</p>}</>
                        : mode === 'error' ? <p className="break-words">{error}</p>
                            : <><p>{mode === 'checking' ? '진행 중인 작업이 있는지 확인하고 있습니다.' : status.shutdown_requested ? (status.shutdown_stage || '백엔드 종료 확인 중') : '종료 요청의 접수 여부를 확인하고 있습니다.'}</p><p className="mt-2">작업을 안전하게 마무리하면 창이 자동으로 닫힙니다.</p><p className="mt-2 flex items-center gap-2"><Loader2 className="h-4 w-4 shrink-0 animate-spin" />{elapsed}초 대기 중</p>{elapsed >= 30 && <p className="mt-3">작업이 오래 걸리거나 연결 정리가 지연될 수 있습니다. 이 화면이 계속되면 표시된 단계와 대기 시간을 알려주세요. 강제로 종료하면 저장 중인 정보가 손상될 수 있습니다.</p>}</>}
                </div>
                {(mode === 'confirm' || mode === 'error') && <div className="mt-6 flex flex-wrap justify-end gap-3">
                    <button ref={keepRef} onClick={() => changeMode(null)} className="rounded-xl bg-zinc-100 px-5 py-3 text-sm font-semibold text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">{mode === 'error' ? '안내 닫기' : '창 유지하기'}</button>
                    <button onClick={begin} className="rounded-xl bg-blue-600 px-5 py-3 text-sm font-semibold text-white hover:bg-blue-700">{mode === 'confirm' ? '마무리 후 종료' : '종료 다시 확인'}</button>
                </div>}
            </section>
        </div>}
    </>;
}

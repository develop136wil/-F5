import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { settingsDrafts } from '../../utils/settingsDrafts';
import GuidedSetup from './GuidedSetup';

export default function SetupDialog() {
    const location = useLocation();
    const navigate = useNavigate();
    const dialog = useRef(null);
    const [confirmClose, setConfirmClose] = useState(false);
    useSyncExternalStore(settingsDrafts.subscribe, settingsDrafts.getVersion);
    const writing = settingsDrafts.isWriting();
    const dirty = settingsDrafts.dirtyNames().length > 0;
    const close = () => {
        const params = new URLSearchParams(location.search);
        params.delete('step'); params.delete('edit');
        navigate(`${location.pathname}?${params}`, { replace: true });
    };
    const requestClose = () => {
        if (writing) return;
        if (dirty) setConfirmClose(true);
        else close();
    };
    useEffect(() => {
        const previous = document.activeElement;
        const element = dialog.current;
        element.showModal();
        return () => { element.close(); previous?.focus?.(); };
    }, []);
    return <dialog ref={dialog} aria-labelledby="setup-dialog-title" onCancel={event => { event.preventDefault(); requestClose(); }}
        className="m-auto w-[min(900px,calc(100vw-32px))] max-h-[calc(100dvh-32px)] overflow-y-auto rounded-3xl border border-gray-200 bg-white p-0 text-gray-900 shadow-2xl backdrop:bg-slate-900/40 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100">
        <header data-setup-sticky className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b bg-white px-6 py-4 dark:border-gray-700 dark:bg-gray-800">
            <h1 id="setup-dialog-title" className="text-[28px] font-bold leading-tight">연결·설정 도우미</h1>
            <button autoFocus disabled={writing} onClick={requestClose} className="rounded-lg border px-4 py-2 text-sm disabled:opacity-50">{writing ? '저장 중…' : '닫기'}</button>
        </header>
        {confirmClose && <div role="alert" className="m-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm">
            <p>아직 저장하지 않은 입력이 있습니다. 닫아도 앱 실행 중에는 유지되지만, 앱을 종료하면 사라집니다.</p>
            <div className="mt-3 flex gap-3"><button onClick={() => setConfirmClose(false)} className="rounded-lg border px-3 py-2">계속 설정</button><button disabled={writing} onClick={close} className="rounded-lg border px-3 py-2">입력 유지하고 닫기</button></div>
        </div>}
        <div className="p-4"><GuidedSetup /></div>
    </dialog>;
}

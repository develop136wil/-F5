import React, {useEffect, useRef} from 'react';

export default function PolicyUndoDialog({request, busy, onClose, onConfirm}) {
    const ref=useRef(null);
    useEffect(()=>{
        const previous=document.activeElement;
        const dialog=ref.current;
        dialog.showModal();
        return ()=>{dialog.close();previous?.focus?.();};
    },[]);
    const field=request.field;
    return <dialog ref={ref} aria-labelledby="policy-undo-title" onCancel={e=>{e.preventDefault();if(!busy)onClose();}}
        className="m-auto w-[min(680px,calc(100vw-32px))] max-h-[calc(100dvh-32px)] overflow-y-auto rounded-2xl border border-gray-200 bg-white p-0 text-gray-800 shadow-xl backdrop:bg-slate-900/40">
        <header className="border-b border-gray-100 px-6 py-5">
            <h2 id="policy-undo-title" className="text-xl font-semibold">적용 전 내용으로 되돌리기</h2>
            <p className="mt-2 break-words text-sm text-gray-500">{request.title} · {field.field}</p>
        </header>
        <div className="space-y-4 px-6 py-5">
            <div className="grid gap-3 sm:grid-cols-2">
                <section className="min-w-0 rounded-xl border border-gray-200 bg-gray-50/50 p-4">
                    <h3 className="mb-2 text-sm font-semibold text-gray-600">{field.needs_verification ? '반영 요청한 내용' : '앱에서 반영한 내용'}</h3>
                    <p className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-6">{field.styled || field.new || '(없음)'}</p>
                </section>
                <section className="min-w-0 rounded-xl border border-indigo-100 bg-indigo-50/40 p-4">
                    <h3 className="mb-2 text-sm font-semibold text-indigo-700">되돌릴 내용</h3>
                    <p className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-6">{(field.undo_before ?? field.old) || '(없음)'}</p>
                </section>
            </div>
            <p className="text-xs leading-5 text-gray-500">이 속성만 되돌립니다. Notion의 현재 내용이 앱에서 반영한 값과 다르면 취소하지 않습니다.</p>
            {field.needs_verification && <p className="text-xs leading-5 text-amber-700">반영 결과가 확정되지 않은 기록입니다. 서버가 Notion의 현재 값과 기록을 대조한 뒤 일치할 때만 되돌립니다.</p>}
        </div>
        <footer className="flex flex-wrap justify-end gap-2 border-t border-gray-100 px-6 py-4">
            <button autoFocus type="button" disabled={busy} onClick={onClose} className="min-h-10 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">돌아가기</button>
            <button type="button" disabled={busy || field.undo_available === false} onClick={onConfirm} className="min-h-10 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50">{busy?'되돌리는 중…':'적용 취소하기'}</button>
        </footer>
    </dialog>;
}

import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle } from 'lucide-react';

export default function NewsActionDialog({ confirmation, onAnswer }) {
    const dialogRef = useRef(null);
    const titleId = useId();
    const descriptionId = useId();
    useEffect(() => {
        const previous = document.activeElement;
        const dialog = dialogRef.current;
        dialog.showModal();
        return () => {
            if (dialog.open) dialog.close();
            if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
        };
    }, []);

    return createPortal(<dialog ref={dialogRef} aria-labelledby={titleId} aria-describedby={descriptionId} aria-modal="true"
        onCancel={event => { event.preventDefault(); onAnswer(false); }}
        onKeyDown={event => {
            if (event.key !== 'Tab') return;
            const actions = [...event.currentTarget.querySelectorAll('button:not([disabled])')];
            const first = actions[0], last = actions[actions.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault(); last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault(); first?.focus();
            }
        }}
        className="m-auto max-h-[85vh] w-[calc(100%-3rem)] max-w-lg overflow-y-auto rounded-2xl border-0 bg-white p-6 text-gray-900 shadow-xl backdrop:bg-black/40 dark:bg-gray-900 dark:text-gray-100">
        <AlertTriangle className="mb-4 h-8 w-8 text-amber-500" aria-hidden="true" />
        <h2 id={titleId} className="text-xl font-semibold leading-7 break-words">{confirmation.title}</h2>
        <p id={descriptionId} className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-300 break-words">{confirmation.description}</p>
        {confirmation.note && <p className="mt-3 rounded-xl bg-blue-50 p-3 text-sm leading-6 text-blue-700 dark:bg-blue-950/30 dark:text-blue-300">{confirmation.note}</p>}
        <div className="mt-6 flex flex-wrap justify-end gap-2">
            <button type="button" autoFocus onClick={() => onAnswer(false)} className="min-h-11 rounded-xl bg-gray-100 px-4 py-2 text-sm font-semibold text-gray-700 focus-visible:outline-blue-500 dark:bg-gray-800 dark:text-gray-200">취소</button>
            <button type="button" onClick={() => onAnswer(true)} className="min-h-11 rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 focus-visible:outline-blue-500">{confirmation.confirmLabel}</button>
        </div>
    </dialog>, document.body);
}

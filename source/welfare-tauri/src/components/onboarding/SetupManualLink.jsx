import React, { useState } from 'react';
import { MANUAL_TOPICS, NEWS_MANUAL_NOTE, manualTopic } from '../../constants/setupManual';

export default function SetupManualLink({ kind, phase }) {
    const topic = manualTopic(kind, phase);
    const section = MANUAL_TOPICS[topic];
    const [error, setError] = useState('');
    const [pending, setPending] = useState(false);
    const open = async () => {
        setError(''); setPending(true);
        try {
            if (window.__TAURI_INTERNALS__) {
                const { invoke } = await import('@tauri-apps/api/core');
                await invoke('open_setup_manual', { topic });
            } else {
                const opened = window.open(`/manual.html?topic=${topic}`, 'welfare-f5-manual', 'width=720,height=560,resizable=yes,scrollbars=yes');
                if (!opened) throw new Error('popup blocked');
                opened.focus();
            }
        } catch { setError('안내 창을 열지 못했습니다. 다시 시도해주세요. 브라우저에서는 팝업 허용을 확인해주세요.'); }
        finally { setPending(false); }
    };
    return <aside className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
        <div className="flex flex-wrap items-center justify-between gap-3">
            <div><p className="text-sm font-semibold">{section.title} 매뉴얼</p></div>
            <button type="button" disabled={pending} onClick={open} className="min-h-10 rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium hover:bg-gray-50 focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:hover:bg-gray-800">{pending ? '여는 중…' : '설정 매뉴얼 열기'}</button>
        </div>
        {kind === 'news' && <p className="mt-3 text-sm leading-6 text-teal-800 dark:text-teal-200">{NEWS_MANUAL_NOTE}</p>}
        {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </aside>;
}

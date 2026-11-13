import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MANUAL_PDF, MANUAL_TOPICS, NEWS_MANUAL_NOTE } from './constants/setupManual';
import './manual.css';

function Manual() {
    const requested = new URLSearchParams(location.search).get('topic');
    const topic = Object.hasOwn(MANUAL_TOPICS, requested) ? requested : 'google';
    const section = MANUAL_TOPICS[topic];
    const [index, setIndex] = useState(0);
    const [zoom, setZoom] = useState(false);
    const [message, setMessage] = useState('');
    const [busy, setBusy] = useState(false);
    const [failed, setFailed] = useState(false);
    const page = section.pages[index];
    const move = next => { setIndex(Math.max(0, Math.min(section.pages.length - 1, next))); setFailed(false); setZoom(false); };
    useEffect(() => {
        const keydown = e => {
            if (e.altKey || e.ctrlKey || e.metaKey || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
            if (e.key === 'Escape' && zoom) { e.preventDefault(); setZoom(false); }
            if (e.key === 'ArrowRight') { e.preventDefault(); move(index + 1); }
            if (e.key === 'ArrowLeft') { e.preventDefault(); move(index - 1); }
        };
        document.addEventListener('keydown', keydown);
        return () => document.removeEventListener('keydown', keydown);
    }, [index, zoom]);
    const close = async () => {
        if (!window.__TAURI_INTERNALS__) { window.close(); return; }
        try { const { getCurrentWindow } = await import('@tauri-apps/api/window'); await getCurrentWindow().close(); }
        catch { setMessage('창 위쪽의 X 버튼으로 닫아주세요.'); }
    };
    const download = async () => {
        setBusy(true); setMessage('');
        try {
            const { invoke } = await import('@tauri-apps/api/core');
            await invoke('export_setup_manual', { open: false });
            setMessage('다운로드 폴더에 매뉴얼을 저장했습니다.');
        } catch { setMessage('매뉴얼을 저장하지 못했습니다. 다시 시도해주세요.'); }
        finally { setBusy(false); }
    };
    return <div className="manual-shell">
        <header><div><h1>설정 매뉴얼</h1><p>{section.title}</p></div><button onClick={close}>닫기</button></header>
        <main aria-label="매뉴얼 슬라이드">
            <div className={`slide ${zoom ? 'zoomed' : ''}`}>
                {failed ? <p role="alert">페이지를 불러오지 못했습니다. PDF를 다운로드해 확인해주세요.</p>
                    : <img key={page} src={`/manual/page-${page}.webp`} alt={`${section.title} · PDF ${page}쪽`} onError={() => setFailed(true)}/>}
            </div>
            <button className="arrow previous" aria-label="이전 페이지" disabled={index === 0} onClick={() => move(index - 1)}>‹</button>
            <button className="arrow next" aria-label="다음 페이지" disabled={index === section.pages.length - 1} onClick={() => move(index + 1)}>›</button>
        </main>
        <nav aria-label="매뉴얼 페이지 선택">
            {section.pages.map((number, i) => <button key={number} className="dot" aria-label={`PDF ${number}쪽으로 이동`} aria-current={index === i ? 'page' : undefined} onClick={() => move(i)}><span/></button>)}
            <span className="counter" aria-live="polite">{index + 1} / {section.pages.length}</span>
        </nav>
        {topic.startsWith('news') && <p className="news-note">{NEWS_MANUAL_NOTE}</p>}
        <footer><button aria-pressed={zoom} onClick={() => setZoom(v => !v)}>{zoom ? '화면에 맞추기' : '확대 보기'}</button>
            {window.__TAURI_INTERNALS__ ? <button disabled={busy} onClick={download}>{busy ? '저장 중…' : 'PDF 다운로드'}</button> : <a href={MANUAL_PDF} download="복지F5 매뉴얼.pdf">PDF 다운로드</a>}
        </footer>
        {message && <p role="status" className="message">{message}</p>}
    </div>;
}
createRoot(document.getElementById('root')).render(<Manual/>);

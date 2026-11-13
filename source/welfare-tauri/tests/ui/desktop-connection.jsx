import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import DesktopLifecycle from '../../src/components/DesktopLifecycle';
import '../../src/index.css';

function Editor() {
    const [value, setValue] = useState('초안');
    useEffect(() => { window.connectionFixture.mounts++; }, []);
    return <main className="p-8"><h1>설정 편집 시험</h1>
        <label>찾을 단어<input value={value} onChange={e => setValue(e.target.value)} className="block border p-3" /></label>
        <button onClick={() => window.connectionFixture.saves++} className="mt-4 rounded border p-3">시험 저장</button>
        <div style={{height: 1200}}>스크롤 유지 시험</div>
    </main>;
}
createRoot(document.getElementById('root')).render(<DesktopLifecycle><Editor /></DesktopLifecycle>);

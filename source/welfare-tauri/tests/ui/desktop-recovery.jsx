import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import '../../src/index.css';

window.__TAURI__ = {};
window.testStatus = {phase:'starting'};
window.mounts = 0;
window.actions = 0;
window.shutdownCalls = 0;
window.closeAcks = [];
const callbacks = new Map();
let callbackId = 0;
window.__TAURI_INTERNALS__ = {
    transformCallback: callback => { callbacks.set(++callbackId, callback); return callbackId; },
    invoke: async (name, args) => {
        if (name === 'desktop_status') {
            if (window.hangStatus) return new Promise(resolve => { window.resolveStatus = resolve; });
            if (window.rejectStatus) throw new Error('시험용 연결 지연');
            return window.testStatus;
        }
        if (name === 'desktop_shutdown') { window.shutdownCalls++; return; }
        if (name === 'desktop_close_ack') { window.closeAcks.push(args.requestId); return; }
        if (name === 'plugin:event|listen') {
            window.requestClose = () => callbacks.get(args.handler)({event:args.event,id:1,payload:1});
            return 1;
        }
        throw new Error(`Unexpected command: ${name}`);
    },
};
const {default: DesktopLifecycle} = await import('../../src/components/DesktopLifecycle.jsx');
function Draft() {
    const [text,setText] = useState('');
    useEffect(()=>{window.mounts++;},[]);
    return <main><label>시험 입력<input aria-label="시험 입력" value={text} onChange={e=>setText(e.target.value)}/></label><button onClick={()=>window.actions++}>시험 작업</button></main>;
}
createRoot(document.getElementById('root')).render(<DesktopLifecycle><Draft/></DesktopLifecycle>);

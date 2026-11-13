import React from 'react';
import {createRoot} from 'react-dom/client';
import NewsConditionWizard from '../../src/components/NewsConditionWizard';
import '../../src/index.css';
window.saves=[];window.cancelCount=0;
createRoot(document.getElementById('root')).render(<NewsConditionWizard onClose={()=>window.cancelCount++} onSave={async value=>{window.saves.push(value);await new Promise(resolve=>setTimeout(resolve,200));if(window.saves.length===1)throw Error('시험 저장 실패');}}/>);

import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import '../../src/index.css';
import KeywordListEditor from '../../src/components/KeywordListEditor';
function Fixture(){
    const [words,setWords]=useState(['돌봄','부모 교육',...Array.from({length:71},(_,i)=>`시험 단어 ${i+1}`)]);
    const [saved,setSaved]=useState(null),[locked,setLocked]=useState(false);
    window.keywordDraft=words;window.keywordSaved=saved;
    return <main className="mx-auto max-w-3xl p-4 text-gray-800 dark:text-gray-100">
        <fieldset disabled={locked} className="ui-card min-w-0 p-4"><KeywordListEditor id="words" label="찾을 단어" value={words} onChange={setWords} description="관련 소식을 찾고 판단할 때 참고합니다."/></fieldset>
        <button type="button" onClick={()=>setSaved([...words])}>최종 저장</button>
        <button type="button" onClick={()=>setLocked(value=>!value)}>잠금 전환</button>
    </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);

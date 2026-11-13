import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import '../../src/index.css';
import NewsConditionDetails from '../../src/components/NewsConditionDetails';

function Fixture(){
    const [draft,setDraft]=useState({
        keywords:['영유아','육아','양육','돌봄','부모 교육','가족 지원','보육','아동',...Array.from({length:14},(_,i)=>`포함 예시 ${i+1}`)],
        excluded_keywords:['인사발령','직원 채용','입찰','계약 공고','보도자료','휴관 안내','인사 이동','채용 결과',...Array.from({length:65},(_,i)=>`제외 예시 ${i+1}`)],
        ai_prompt:'신청 대상과 신청 방법을 쉬운 말로 설명해주세요.',
    });
    window.detailsDraft=draft;
    return <main className="mx-auto max-w-3xl p-4 text-gray-800 dark:text-gray-100">
        <NewsConditionDetails defaultOpen value={draft} onChange={(field,value)=>setDraft(previous=>({...previous,[field]:value}))}/>
        <button type="button" className="mt-4 rounded-xl bg-blue-600 px-5 py-3 text-sm font-semibold text-white" onClick={()=>{window.detailsSaved=structuredClone(draft);}}>최종 저장</button>
    </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);

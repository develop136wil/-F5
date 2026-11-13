import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import OptionalAgeMonthInput from '../../src/components/OptionalAgeMonthInput';
import '../../src/index.css';

function Fixture(){
    const [min,setMin]=useState(null),[max,setMax]=useState(11),[disabled,setDisabled]=useState(false);
    return <main className="mx-auto max-w-3xl space-y-6 bg-white p-6 text-gray-800 dark:bg-[#1e2128] dark:text-gray-100">
        <h1 className="text-xl font-semibold">나이 선택</h1>
        <OptionalAgeMonthInput label="시작 나이" value={min} onChange={setMin} idPrefix="min" disabled={disabled}/>
        <OptionalAgeMonthInput label="종료 나이" value={max} onChange={setMax} idPrefix="max" disabled={disabled}/>
        <output data-testid="ages">{JSON.stringify({min,max})}</output>
        <div className="flex flex-wrap gap-3 text-sm"><button onClick={()=>{setMin(0);setMax(1799);}}>저장한 나이 다시 읽기</button><button onClick={()=>{setMin(null);setMax(null);}}>저장한 무제한 다시 읽기</button><button onClick={()=>setDisabled(value=>!value)}>입력 잠금 전환</button></div>
    </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);

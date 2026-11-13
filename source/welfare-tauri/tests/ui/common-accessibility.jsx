import React, {useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Button} from '../../src/components/ui/Button';
import {Tooltip} from '../../src/components/ui/Tooltip';
import ItemDetailModal from '../../src/components/ItemDetailModal';
import NewsActionDialog from '../../src/components/NewsActionDialog';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

window.accessibilityFixture={actions:0,submits:0,answers:[],detailCalls:[],similarCalls:[]};
const calls=window.accessibilityFixture;
welfareApi.getItemDetail=async id=>{
    calls.detailCalls.push(id);
    if(id===2) throw new Error('synthetic detail error');
    return {id,title:'키보드로 확인하는 복지 안내',link:'https://example.invalid/source',support_detail:'시험용 안내입니다.'};
};
welfareApi.getSimilarItems=async id=>{calls.similarCalls.push(id);return [];};
function Fixture(){
    const [detail,setDetail]=useState(null);
    const [confirm,setConfirm]=useState(false);
    const buttonRef=useRef(null);
    return <main className="mx-auto max-w-2xl space-y-6 p-8 text-gray-900 dark:text-gray-100">
        <h1 className="text-2xl font-bold">공통 접근성 검사</h1>
        <p id="existing-description">기존 설명 유지</p>
        <div className="flex flex-wrap items-center gap-4">
            <Tooltip content="선택만으로 저장되지 않습니다." side="bottom"><Button type="button" ref={buttonRef} aria-describedby="existing-description" onClick={()=>calls.actions++}>도움말 있는 동작</Button></Tooltip>
            <Tooltip content="저장이 끝난 후 다시 사용할 수 있습니다." side="bottom"><button type="button" disabled className="rounded border px-4 py-2">비활성 동작</button></Tooltip>
            <Tooltip content="기본 링크를 탐색하는 방식입니다." side="bottom"><span className="rounded bg-gray-100 p-2">수집 방식 안내</span></Tooltip>
        </div>
        <form onSubmit={event=>{event.preventDefault();calls.submits++;}} className="flex gap-3">
            <Button type="submit">양식 제출</Button><Button type="button" onClick={()=>buttonRef.current?.focus()}>앞 버튼으로 초점</Button>
        </form>
        <div className="flex flex-wrap gap-3">
            <Button type="button" onClick={()=>setDetail(1)}>상세 열기</Button>
            <Button type="button" onClick={()=>setDetail(2)}>오류 상세 열기</Button>
            <Button type="button" onClick={()=>setConfirm(true)}>승인 대화상자 열기</Button>
        </div>
        <button type="button" id="outside-focus">배경 동작</button>
        {detail&&<ItemDetailModal itemId={detail} onClose={()=>setDetail(null)}/>}
        {confirm&&<NewsActionDialog confirmation={{title:'시험 동작을 실행할까요?',description:'외부 요청 없이 확인 값만 기록합니다.',confirmLabel:'확인하고 실행'}} onAnswer={answer=>{calls.answers.push(answer);setConfirm(false);}}/>}
    </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);

import {useEffect, useRef, useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {Link} from 'react-router-dom';
import {welfareApi} from '../api/welfare';
import {queryKeys} from '../api/queryKeys';
import {isRealLink} from '../utils/link';

const views={processing:'분석 대기·실패',review:'추가 확인',records:'제외·종료'};
const notes={
    processing:'아직 분석을 마치지 못한 자료입니다. 연결된 작업이 있는 자료는 위의 수집 진행에서 재개 여부를 확인하고, 이전 기록은 각 안내를 확인하세요.',
    review:'분석의 근거나 안전 확인이 필요한 자료입니다. 저장할 소식으로 확정된 항목은 아닙니다.',
    records:'제외되거나 종료된 자료의 기록입니다. 찾은 소식에는 다시 표시하지 않습니다.',
};
const button='min-h-10 rounded-lg border border-gray-200 px-3 py-2 disabled:opacity-40';

// Read-only diagnostics are lazy-loaded, never mixed into the selectable feed.
export default function NewsProcessingRecords({profileId,counts,disabled=false,onDetail,openRequest}) {
    const [open,setOpen]=useState(false);
    const section=useRef(null);
    useEffect(()=>{
        if(!openRequest)return;
        setOpen(true);
        const frame=requestAnimationFrame(()=>{
            section.current?.scrollIntoView({block:'start',behavior:'auto'});
            section.current?.focus({preventScroll:true});
        });
        return()=>cancelAnimationFrame(frame);
    },[openRequest]);
    // Only a confirmed empty diagnostic count may hide this entry. Missing or
    // failed metadata must not hide records when the ready list is empty.
    if (!openRequest && Object.keys(views).every(view => counts?.[view] === 0)) return null;
    return <details ref={section} tabIndex={-1} open={open} className="text-sm text-gray-500 focus:outline-none" onToggle={event=>setOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer py-2" onClick={event=>{event.preventDefault();setOpen(value=>!value);}}>처리 기록</summary>
        {open&&<Records key={openRequest?.id} profileId={profileId} disabled={disabled} onDetail={onDetail} initialView={openRequest?.view}/>}
    </details>;
}

function Records({profileId,disabled,onDetail,initialView='processing'}) {
    const [view,setView]=useState(initialView),[offset,setOffset]=useState(0);
    const heading=useRef(null);
    const query=useQuery({queryKey:[...queryKeys.newsFeed,profileId,view,offset],
        queryFn:({signal})=>welfareApi.getNewsFeed({profileId,view,offset,signal}),retry:false});
    const data=query.data,items=data?.items||[];
    const blocked=disabled||query.isFetching;
    useEffect(()=>{
        if(data&&!query.isFetching&&offset>0&&offset>=data.total)
            setOffset(Math.max(0,Math.floor((Math.max(1,data.total)-1)/10)*10));
    },[data,query.isFetching,offset]);
    const changePage=next=>{setOffset(next);requestAnimationFrame(()=>{
        heading.current?.scrollIntoView({block:'start',behavior:'auto'});
        heading.current?.focus({preventScroll:true});
    });};
    return <div aria-label="새 소식 처리 기록" className="space-y-3 pt-2">
        <p>문제가 있을 때만 확인하세요. 기록을 보는 것만으로 AI를 사용하거나 소식을 추가하지 않습니다.</p>
        <div ref={heading} tabIndex={-1} className="flex flex-wrap gap-2 focus:outline-none">
            {Object.entries(views).map(([key,label])=><button key={key} type="button" disabled={disabled} aria-pressed={view===key}
                onClick={()=>{setView(key);setOffset(0);}} className={`${button} aria-pressed:border-teal-600 aria-pressed:text-teal-800`}>{label}</button>)}
            <button type="button" disabled={blocked} onClick={()=>query.refetch()} className={button}>기록 새로고침</button>
        </div>
        <p>{notes[view]}</p>
        {view==='processing'&&items.some(item=>item.workflow_id)&&<button type="button" className="text-blue-600 underline" onClick={()=>{
            const panel=document.getElementById('news-collection');
            panel?.scrollIntoView({block:'start',behavior:'auto'});panel?.focus({preventScroll:true});
        }}>수집 진행으로 이동</button>}
        {query.isLoading?<p role="status">처리 기록을 불러오고 있습니다.</p>:query.isError?
            <p role="alert">처리 기록을 불러오지 못했습니다. 기록 새로고침으로 다시 확인해주세요.</p>:
            !items.length?<p>이 분류에 해당하는 기록이 없습니다.</p>:<ul className="space-y-2">{items.map(item=><li key={item.id} className="rounded-xl border border-gray-100 bg-white p-4 dark:border-gray-700 dark:bg-[#1e2128]">
                <button type="button" onClick={()=>onDetail(item)} className="break-words text-left text-sm font-medium text-gray-800 dark:text-gray-100">{item.title||'제목 확인 필요'}</button>
                <p className="mt-1 text-xs">{item.status||views[view]}</p>
                {item.input_note&&<p className="mt-1 break-words text-xs leading-5">{item.input_note}</p>}
                {item.recovery_action==='new_collection'&&!item.workflow_id&&<div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                    {item.review_reason==='authentication_required'&&<Link to="/settings?tab=guide&step=google" className="text-blue-600 underline">AI 연결 확인</Link>}
                    <Link to="/settings?tab=keywords" className="text-blue-600 underline">수집 조건 확인·저장</Link>
                </div>}
                {isRealLink(item.link)&&<a href={item.link} target="_blank" rel="noopener noreferrer" className="mt-2 inline-block text-xs text-blue-600 underline">원문 보기</a>}
            </li>)}</ul>}
        <nav aria-label="처리 기록 페이지" className="flex flex-wrap items-center gap-3">
            <button type="button" disabled={blocked||offset===0} onClick={()=>changePage(Math.max(0,offset-10))} className={button}>이전 10건</button>
            <span>{Number.isInteger(data?.total)?data.total?`${offset+1}–${Math.min(offset+10,data.total)} / ${data.total}건`:'0건':'확인 중'}</span>
            <button type="button" disabled={blocked||!data?.has_more} onClick={()=>changePage(offset+10)} className={button}>다음 10건</button>
        </nav>
        <Link to="/search" className="inline-block text-blue-600 underline">기존 저장 자료 검색</Link>
    </div>;
}

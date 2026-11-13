import React from 'react';
import {newsTaskProgress, compactNewsTaskStatus} from '../utils/newsTaskProgress';

export default function NewsTaskProgress({task, unavailable = false, showNotice = true, compact = false}) {
    const view=newsTaskProgress(task,unavailable);
    const compactStatus=compactNewsTaskStatus(task,unavailable);
    return <div aria-label="새 소식 진행 상황" className="space-y-2 text-xs">
        <p role="status" className="font-semibold text-blue-800 dark:text-blue-200">{compact ? `새 소식 · ${compactStatus}` : view.title}</p>
        {!compact&&view.siteName&&<p className="break-words text-gray-600 dark:text-gray-300">{view.active?'확인 중인 기관':'마지막 확인 기관'} · {view.siteName}</p>}
        {(view.percent!==null||view.active)&&<div role="progressbar" aria-label={view.phaseLabel}
            aria-valuenow={view.percent??undefined} aria-valuemin={0} aria-valuemax={100}
            aria-valuetext={view.percent===null?'전체 분량 확인 중':`${view.amount} · 현재 단계 ${view.percent}%`}
            className="h-1.5 overflow-hidden rounded-full bg-blue-100 dark:bg-blue-900/40">
            <div className={`h-full rounded-full bg-blue-500 ${view.percent===null&&view.active?'animate-pulse motion-reduce:animate-none':''}`}
                style={{width:view.percent===null?'33%':`${view.percent}%`}}/>
        </div>}
        <p className="text-gray-700 dark:text-gray-200">{compact&&unavailable?'마지막 확인 · ':''}{view.amount}{view.percent!==null?` · ${view.percent}%`:''}</p>
        {!compact&&view.phaseNote&&<p className="text-[11px] leading-4 text-gray-500">{view.phaseNote}</p>}
        {!compact&&view.lines.length>0&&<p className="break-words leading-5 text-gray-600 dark:text-gray-300">{view.lines.join(' · ')}</p>}
        {!compact&&(unavailable||showNotice)&&view.note&&<p className="break-words leading-5 text-gray-600 dark:text-gray-300">{view.note}</p>}
    </div>;
}

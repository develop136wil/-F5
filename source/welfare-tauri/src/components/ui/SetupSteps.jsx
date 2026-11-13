import React from 'react';
import {Check} from 'lucide-react';

export default function SetupSteps({labels, current, label}) {
    return <ol aria-label={label} className="flex items-start py-2">
        {labels.map((name,index)=><li key={name} aria-current={index===current?'step':undefined} className="relative flex min-w-0 flex-1 flex-col items-center gap-2 text-sm">
            {index<labels.length-1&&<span aria-hidden="true" className={`absolute left-1/2 top-4 h-px w-full ${index<current?'bg-blue-500':'bg-gray-200 dark:bg-gray-600'}`}/>}
            <span aria-hidden="true" className={`relative flex h-8 w-8 items-center justify-center rounded-full border text-sm font-semibold ${index===current?'border-blue-600 bg-blue-600 text-white':index<current?'border-blue-500 bg-blue-50 text-blue-700 dark:bg-gray-800 dark:text-blue-300':'border-gray-300 bg-white text-gray-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-400'}`}>
                {index<current?<Check size={16}/>:index+1}
            </span>
            <span className={index===current?'font-semibold text-blue-700 dark:text-blue-300':'text-gray-600 dark:text-gray-300'}><span className="sr-only">{index+1}단계 · {index<current?'완료 · ':index===current?'현재 · ':'다음 · '}</span>{name}</span>
        </li>)}
    </ol>;
}

import {useId} from 'react';
import AgeMonthInput from './AgeMonthInput';

export default function OptionalAgeMonthInput({value, onChange, idPrefix, suffix, label, disabled=false}) {
    const empty = value === '' || value === null || value === undefined;
    const inputId = useId();
    return <fieldset disabled={disabled} className="min-w-0 space-y-3 disabled:opacity-50">
        <legend className="mb-2 text-sm font-semibold text-gray-800 dark:text-gray-100">{label}</legend>
        <div className="inline-flex max-w-full flex-wrap gap-1 rounded-xl bg-gray-100 p-1 dark:bg-gray-800" role="radiogroup" aria-label={`${label} 제한 선택`}>
            {[['unlimited','제한 없음',empty],['specified','나이 지정',!empty]].map(([mode,text,checked])=><label key={mode}
                className={`inline-flex min-h-10 cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm ${checked ? 'bg-blue-600 font-semibold text-white' : 'text-gray-600 dark:text-gray-300'} ${disabled ? 'cursor-not-allowed' : ''}`}>
                <input type="radio" name={`${inputId}-mode`} value={mode} checked={checked} onChange={()=>onChange(mode==='unlimited'?'':0)}
                    className="h-4 w-4 accent-indigo-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400" />
                {text}
            </label>)}
        </div>
        {empty ? <p className="text-xs text-gray-500">{label==='시작 나이'?'시작 나이의 제한 없이 찾습니다.':label==='종료 나이'?'종료 나이의 제한 없이 찾습니다.':'나이를 제한하지 않습니다.'}</p> :
            <AgeMonthInput value={value} onChange={onChange} idPrefix={idPrefix||inputId} maxMonths={1800} disabled={disabled} suffix={suffix??(label==='시작 나이'?'부터':'까지')} accessibleLabel={label}/>} 
    </fieldset>;
}

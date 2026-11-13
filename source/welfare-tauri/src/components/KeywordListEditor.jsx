import React, {useEffect, useRef, useState} from 'react';
import {ChevronDown, Plus, Search, Undo2, X} from 'lucide-react';
import {parseTags} from '../utils/newsProfileForm';
import {appendKeywords, restoreKeyword} from '../utils/keywordList';

export default function KeywordListEditor({id, label, value=[], onChange, description}) {
    const [entry, setEntry] = useState('');
    const [query, setQuery] = useState('');
    const [bulkText, setBulkText] = useState(() => value.join('\n'));
    const [removed, setRemoved] = useState(null);
    const [notice, setNotice] = useState('');
    const [expanded, setExpanded] = useState(false);
    const inputRef = useRef(null), listRef = useRef(null), composing = useRef(false);
    useEffect(() => {
        setBulkText(previous => JSON.stringify(parseTags(previous)) === JSON.stringify(value) ? previous : value.join('\n'));
    }, [value]);
    const matching = value.filter(word => word.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
    const visible = expanded || query.trim() ? matching : matching.slice(0, 8);
    function add(text = entry) {
        if (composing.current || !text.trim()) return;
        if (!parseTags(text).length) { setEntry(''); setNotice('추가할 단어를 입력해주세요.'); return; }
        const result = appendKeywords(value, text);
        if (result.added.length) {
            onChange(result.words);
            setExpanded(true);
            setQuery('');
        }
        setEntry('');
        setNotice(result.added.length ? `${result.added.length}개를 추가했습니다. 최종 저장 후 적용됩니다.` : '이미 등록된 단어입니다. 중복으로 추가하지 않았습니다.');
    }
    function remove(word) {
        const focusIndex = visible.indexOf(word);
        setRemoved({word, index:value.indexOf(word)});
        onChange(value.filter(item => item !== word));
        setNotice(`‘${word}’ 삭제. 최종 저장 전에는 되돌릴 수 있습니다.`);
        requestAnimationFrame(() => {
            const buttons = listRef.current?.querySelectorAll('button');
            (buttons?.[Math.min(focusIndex, buttons.length - 1)] || inputRef.current)?.focus();
        });
    }
    return <section aria-labelledby={`${id}-heading`} className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
            <h4 id={`${id}-heading`} className="text-sm font-semibold">{label}</h4>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs tabular-nums text-gray-600 dark:bg-gray-800 dark:text-gray-300">{value.length}개</span>
        </div>
        <p id={`${id}-hint`} className="ui-description">{description}</p>
        {(value.length >= 12 || query) && <div className="relative">
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3.5 h-4 w-4 text-gray-400"/>
            <label htmlFor={`${id}-filter`} className="sr-only">{label} 목록에서 찾기</label>
            <input id={`${id}-filter`} type="search" value={query} onChange={event=>setQuery(event.target.value)} placeholder="등록한 단어 찾기" className="ui-field min-h-11 pl-9"/>
        </div>}
        <div className="min-w-0 rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900/25">
        <div id={`${id}-tags`}>
        {!!value.length && <ul ref={listRef} aria-label={`${label} 목록`} className="flex min-w-0 flex-wrap items-start gap-2">
            {visible.map(word=><li key={word} className="flex min-w-0 max-w-full items-center rounded-3xl bg-gray-100 pl-3.5 text-sm text-gray-700 dark:bg-gray-800 dark:text-gray-200">
                <span className="min-w-0 break-words py-2 [overflow-wrap:anywhere]">{word}</span>
                <button type="button" aria-label={`${label}: ${word} 삭제`} onClick={()=>remove(word)}
                    title={`${word} 삭제`}
                    className="group inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-gray-500 hover:bg-gray-200 hover:text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white">
                    <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-gray-200/80 dark:bg-gray-700"><X aria-hidden="true" className="h-3.5 w-3.5"/></span>
                </button>
            </li>)}
        </ul>}
        {!value.length && <p className="py-2 text-sm text-gray-500 dark:text-gray-400">등록한 단어가 없습니다. 아래에서 필요한 단어를 추가하세요.</p>}
        {query && <p className="ui-meta pt-2" role="status">전체 {value.length}개 중 {matching.length}개 표시 · 목록 검색은 수집 조건을 바꾸지 않습니다.</p>}
        </div>
        {value.length > 8 && !query.trim() && <button type="button" aria-expanded={expanded} aria-controls={`${id}-tags`}
            onClick={()=>setExpanded(previous=>!previous)}
            className="mt-1 inline-flex min-h-11 items-center gap-1 rounded-lg px-1 text-sm font-medium text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300">
            {expanded ? '간단히 보기' : `전체 ${value.length}개 보기`}
            <ChevronDown aria-hidden="true" className={`h-4 w-4 transition-transform motion-reduce:transition-none ${expanded?'rotate-180':''}`}/>
        </button>}
        <div className="mt-3 flex items-start gap-2 border-t border-gray-100 pt-3 dark:border-gray-800">
            <div className="min-w-0 flex-1">
                <label htmlFor={`${id}-add`} className="sr-only">{label} 추가 입력</label>
                <input ref={inputRef} id={`${id}-add`} className="ui-field min-h-11" value={entry}
                    aria-describedby={`${id}-entry-hint`} placeholder="단어 입력" autoComplete="off"
                    onChange={event=>setEntry(event.target.value)} onBlur={()=>add()}
                    onPaste={event=>{
                        const text=event.clipboardData.getData('text').replace(/\r\n?/g,'\n');
                        if(composing.current || !/[,\r\n]/.test(text)) return;
                        event.preventDefault();
                        const input=event.currentTarget;
                        add(entry.slice(0,input.selectionStart ?? entry.length)+text+entry.slice(input.selectionEnd ?? entry.length));
                    }}
                    onCompositionStart={()=>{composing.current=true;}} onCompositionEnd={()=>{composing.current=false;}}
                    onKeyDown={event=>{
                        if(event.key==='Enter' && !event.nativeEvent.isComposing && !composing.current && event.keyCode!==229){event.preventDefault();add();}
                    }}/>
            </div>
            <button type="button" aria-label={`${label} 추가`} onClick={()=>{add();inputRef.current?.focus();}}
                className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-xl border border-blue-200 bg-blue-50 px-3 text-sm font-semibold text-blue-700 hover:bg-blue-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300 dark:hover:bg-blue-900/40">
                <Plus aria-hidden="true" className="h-4 w-4"/>추가
            </button>
        </div>
        </div>
        <p id={`${id}-entry-hint`} className="ui-meta">Enter 또는 추가 버튼으로 넣으세요. 여러 단어는 쉼표·줄바꿈으로 붙여넣을 수 있습니다. 입력 중인 단어도 입력칸을 벗어나면 추가됩니다.</p>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p role="status" className="ui-meta [overflow-wrap:anywhere]">{notice}</p>
            {removed && <button type="button" onClick={()=>{onChange(restoreKeyword(value,removed));setNotice('삭제한 단어를 되돌렸습니다.');setRemoved(null);inputRef.current?.focus();}}
                className="inline-flex min-h-11 items-center gap-1.5 rounded-lg px-1 text-sm text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300">
                <Undo2 aria-hidden="true" className="h-3.5 w-3.5"/>삭제 되돌리기
            </button>}
        </div>
        <details className="group">
            <summary className="inline-flex min-h-11 cursor-pointer list-none items-center gap-1.5 rounded-lg text-sm text-gray-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-400">
                <ChevronDown aria-hidden="true" className="h-4 w-4 transition-transform group-open:rotate-180 motion-reduce:transition-none"/>{label} 한꺼번에 편집
            </summary>
            <div className="space-y-2 pt-2">
                <label htmlFor={id} className="sr-only">{label}</label>
                <p id={`${id}-bulk-hint`} className="ui-description">쉼표나 줄바꿈으로 구분하세요. 수정한 내용은 위 목록에도 반영되며, 최종 저장 전까지는 초안입니다.</p>
                <textarea id={id} aria-describedby={`${id}-bulk-hint`} className="ui-field resize-y" rows={5} value={bulkText}
                    onChange={event=>{setBulkText(event.target.value);onChange(parseTags(event.target.value));setRemoved(null);setNotice('목록을 수정했습니다. 최종 저장 후 적용됩니다.');}}/>
            </div>
        </details>
    </section>;
}

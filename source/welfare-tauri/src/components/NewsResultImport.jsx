import {useEffect, useRef, useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {welfareApi} from '../api/welfare';
import {queryKeys} from '../api/queryKeys';
import {getErrorMessage} from '../utils/error';
import NewsActionDialog from './NewsActionDialog';

const messages = {
    created: '새 소식 후보에 가져왔습니다. Notion 등록·소식 모음 파일 저장은 새 소식에서 따로 선택해주세요.',
    already_imported: '이미 가져온 기록입니다. 제외하거나 삭제한 자료도 자동으로 다시 추가하지 않습니다.',
    existing: '같은 원문 주소가 이미 보관되어 있습니다. 기존 내용과 등록·제외 상태는 그대로 유지했습니다.',
    ended: '종료된 자료로 확인되어 새 소식 후보에 추가하지 않습니다. 분석 기록은 계속 볼 수 있습니다.',
};

export default function NewsResultImport({profileId, jobId, disabled = false}) {
    const client = useQueryClient();
    const [busy,setBusy]=useState(false), [notice,setNotice]=useState(''), [error,setError]=useState('');
    const [preview,setPreview]=useState(null), [done,setDone]=useState(false);
    const pending=useRef(false), alive=useRef(false), buttonRef=useRef(null), blocked=useRef(disabled);
    blocked.current=disabled;
    useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
    function refreshLists() {
        for(const queryKey of [queryKeys.notionCandidates,queryKeys.summaryStats,['search'],['semanticSearch'],['recentItems']]) {
            // Mark AI-assisted search stale without starting an AI request here.
            void client.invalidateQueries({queryKey, refetchType:queryKey[0]==='semanticSearch'?'none':'active'}).catch(()=>{});
        }
    }
    async function prepare() {
        if(pending.current || blocked.current)return;
        pending.current=true;setBusy(true);setError('');setNotice('');
        try {
            const value=await welfareApi.previewNewsResultImport(profileId,jobId);
            if(!alive.current)return;
            if(value.status!=='ready') {
                if(!messages[value.status])throw Error('상태를 확인하지 못했습니다. 다시 확인해주세요.');
                if(value.status==='already_imported')refreshLists();
                setNotice(messages[value.status]);setDone(true);return;
            }
            if(!/^[a-f0-9]{64}$/.test(value.fingerprint))throw Error('가져오기 준비 정보를 확인하지 못했습니다.');
            setPreview(value);
        } catch(e){if(alive.current)setError(getErrorMessage(e));}
        finally {pending.current=false;if(alive.current)setBusy(false);}
    }
    async function answer(confirmed) {
        if(pending.current)return;
        const current=preview;
        setPreview(null);
        buttonRef.current?.focus();
        if(!confirmed || !current)return;
        if(blocked.current){setError('다른 작업이 끝난 뒤 다시 확인해주세요.');return;}
        pending.current=true;setBusy(true);setError('');
        try {
            const result=await welfareApi.importNewsResult(profileId,jobId,current.fingerprint);
            if(!messages[result.status])throw Error('저장 결과를 확인하지 못했습니다. 다시 확인해주세요.');
            // Even when the panel has closed, invalidate server-derived lists.
            refreshLists();
            if(alive.current){setNotice(messages[result.status]);setDone(true);}
        }catch(e){
            if(alive.current)setError(`${getErrorMessage(e)} 다시 확인해도 같은 원문을 중복으로 추가하지 않습니다.`);
        }finally{pending.current=false;if(alive.current)setBusy(false);}
    }
    return <div className="pt-2 space-y-2">
        {!done && <button ref={buttonRef} type="button" disabled={disabled||busy||!!preview} onClick={prepare}
            className="min-h-10 rounded-lg border border-blue-200 px-3 py-2 text-sm font-medium text-blue-700 disabled:opacity-50 dark:border-blue-800 dark:text-blue-300">
            {busy?'가져오기 확인 중…':'새 소식 후보로 가져오기'}
        </button>}
        {notice&&<p role="status" className="text-sm text-blue-700 dark:text-blue-300">{notice}</p>}
        {error&&<p role="alert" className="text-sm text-red-600 dark:text-red-300">{error}</p>}
        {preview&&<NewsActionDialog onAnswer={answer} confirmation={{title:'이 자료를 새 소식 후보로 가져올까요?',
            description:`${preview.title} · 기준 버전 ${preview.revision}. 대상·기간·첨부파일을 원문에서 확인한 자료만 가져오세요.`,
            note:[preview.note,'이 PC에만 저장합니다. 기존 수집 내용은 덮어쓰지 않고, Notion 등록이나 AI 재분석은 하지 않습니다.'].filter(Boolean).join(' '),
            confirmLabel:'확인했어요 · 가져오기'}}/>}
    </div>;
}

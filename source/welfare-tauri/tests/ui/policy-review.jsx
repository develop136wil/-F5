import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import PolicyManage from '../../src/pages/PolicyManage';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';
const mode = new URLSearchParams(location.search).get('mode');
const draftMode=mode?.startsWith('draft');
window.policyCalls = [];
window.individualCalls = [];
const undoFields = [
    {field:'분류',old:'기존 분류',new:'새 분류',level:'high'},
    {field:'상세 지원 내용',old:'기존 설명',new:'수정 설명',level:'low'},
];
let cleaned=false;
const readyItems=items=>items.map(item=>({...item,apply_ready:true,review_state:'proposal',review_notes:[],fields:item.fields.map(field=>({...field,snapshot_current:true}))}));
for (const key of Object.keys(welfareApi)) if (typeof welfareApi[key] === 'function') welfareApi[key] = async () => {throw Error(`실제 호출 금지: ${key}`);};
welfareApi.applyPolicyField = async (id, field) => {
    if(mode==='undo') {
        const target=undoFields.find(f=>f.field===field);target.applied=true;target.undo_id=`receipt-${field}`;
        const remaining=undoFields.filter(f=>!f.applied).length;
        return {completed:remaining===0,remaining};
    }
    window.individualCalls.push({id,field});
    if(mode==='local-save-individual') {
        window.localRecovery=true;
        throw {response:{status:400,data:{detail:{error:'LOCAL_SAVE_FAILED',message:'반영 후 로컬 저장 실패'}}}};
    }
    if((mode==='review-uncertain'&&window.individualCalls.length===1)||(mode==='partial-review-uncertain'&&window.individualCalls.length===2)) throw {response:{status:400,data:{detail:{error:'UPDATE_UNCERTAIN',message:'현재 내용을 확인해주세요.'}}}};
    if(mode==='clear-review-error'&&window.individualCalls.length===1)throw {response:{status:400,data:{detail:{error:'VALIDATION_ERROR',message:'필드 형식을 확인해주세요.'}}}};
    return {completed:false,remaining:1};
};
welfareApi.getAiStatus = async () => ({busy:false});
welfareApi.getPolicyDbs = async () => ({dbs:mode==='colours'?[...['의료·재활','교육·보육','돌봄·양육','가족 지원','생활 지원'],...(new URLSearchParams(location.search).has('moreShelves')?Array.from({length:35},(_,i)=>`추가 보관함 ${i+1}`):[])].map((name,i)=>({key:String(i),name})):draftMode?[{key:'test',name:'시험 정책표'},{key:'other',name:'다른 정책표'}]:[{key:'test', name:'시험 정책표'}]});
welfareApi.getPolicyChanges = async () => {
    if(mode==='manual-comparison')return {items:readyItems([{id:1,title:'프로그램 신청 안내 (설명용 예시)',intensity:'high',
        source_urls:['https://example.org/manual-policy'],
        fields:[{field:'추가 자격요건',old:'신청 대상: 해당 지역 주민\n신청 방법: 기관 방문 신청',new:'신청 대상: 해당 지역 주민\n신청 방법: 기관 방문 또는 온라인 신청',level:'high',requires_individual_review:true}]}])};
    if(mode==='local-save-individual'&&window.localRecovery)return {items:[{id:1,title:'시험 정책 1',intensity:'low',apply_ready:false,review_state:'completed',review_notes:[],fields:[],
        apply_history:[{field:'분류',old:'기존 분류',new:'새 분류',applied:true,undo_id:'prepared-receipt',needs_verification:true,undo_available:true}]}]};
    if(mode==='history')return {items:[{id:1,title:'새 제안과 이전 기록',intensity:'high',apply_ready:true,review_state:'proposal',review_notes:[],
        fields:[{field:'분류',old:'최근값',new:'새로운 제안',level:'high',snapshot_current:true}],
        apply_history:[{field:'분류',old:'원래값',new:'최근값',applied:true,undo_id:'saved-receipt'}]}]};
    if(['history-only','history-uncertain'].includes(mode))return {items:[{id:1,title:'보존된 반영 기록',intensity:'low',apply_ready:false,review_state:'completed',review_notes:[],fields:[],
        apply_history:[{field:'분류',old:'원래값',new:'최근값',applied:true,undo_id:'saved-receipt',needs_verification:true,undo_available:mode!=='history-uncertain'}]}]};
    if(mode==='undo')return {items:readyItems([{id:1,title:'취소 기능 시험 정책',intensity:'high',fields:structuredClone(undoFields)}])};
    if(draftMode)return {items:[]};
    if (mode === 'error') throw Error('모의 조회 오류');
    const items = Array.from({length:mode==='large'?200:mode==='jump'?40:15}, (_,i) => ({id:i+1,title:`시험 정책 ${i+1}`,intensity:i%2?'info':'high',fields:[{field:'분류', old:mode==='large'?'기존 내용 '.repeat(150):'기존 분류',new:mode==='large'?'변경 내용 '.repeat(150):'새 분류',level:'high'}]}));
    if (mode==='jump' && window.policyCalls.length) {
        const moved = items.shift();
        moved.intensity = 'low';
        items.push(moved);
    }
    if (mode === 'manual') items[0].fields = [{field:'추가 자격요건',old:'기존 조건',new:'검토할 조건',level:'high',requires_individual_review:true}];
    if(['uncertain-bulk','local-save-bulk'].includes(mode)) items[0].fields=[{field:'분류',old:'기존 분류',new:'새 분류',level:'high'},{field:'추가 자격요건',old:'기존 조건',new:'검토할 조건',level:'high',requires_individual_review:true}];
    if(mode==='uncertain-bulk-partial') items[0].fields=[{field:'분류',old:'기존 분류',new:'새 분류',level:'high'},{field:'상세 지원 내용',old:'기존 지원',new:'변경 지원',level:'high'},{field:'추가 자격요건',old:'기존 조건',new:'검토할 조건',level:'high',requires_individual_review:true}];
    if(['review-uncertain','partial-review-uncertain','clear-review-error'].includes(mode))items[0].fields=['추가 자격요건','상세 지원 내용','대상 소득 수준'].slice(0,mode==='partial-review-uncertain'?3:2).map(field=>({field,old:'기존 내용',new:'검토할 내용',level:'high',requires_individual_review:true}));
    if (mode === 'visual' || mode === 'colours') items[0].fields = [
        {field:'세부 연령',old:'전 연령',new:'',level:'info'},
        {field:'추가 자격요건',old:'등록 대상자\n증빙 서류 제출 필요',new:'등록 대상자 · 증빙 서류 확인 필요',level:'high',requires_individual_review:true},
        {field:'상세 지원 내용',old:'기존 지원 내용',new:'변경된 지원 내용',level:'info'},
    ];
    if (mode === 'grouped') items[0].fields = [
        {field:'세부 연령',old:'전 연령',new:'',level:'info'},
        {field:'추가 자격요건',old:'등록 대상자\n증빙 서류 제출 필요',new:'등록 대상자 · 증빙 서류 확인 필요',level:'high',requires_individual_review:true},
        {field:'상세 지원 내용',old:'기존 지원 내용',new:'표현을 정리한 지원 내용',level:'low'},
        {field:'대상 소득 수준',old:'소득 무관',new:'None',level:'info'},
        {field:'거주지',old:'서울',new:'전국',level:'info',applied:true},
    ];
    const currentItems=readyItems(items);
    if(mode==='compact-notices')for(const item of currentItems){
        item.apply_ready=false;item.review_state='needs_revalidation';
        item.fields.forEach(field=>delete field.snapshot_current);
        item.review_notes=item.id%2?[{note:'원문이 분석 범위보다 깁니다. 상세 출처 연결 후 다시 점검해주세요.',code:'source_too_long'},{note:'중복된 긴 원문 안내',code:'source_too_long'}]:[];
    }
    if(['stale','source-review','unknown-state'].includes(mode)) {
        currentItems[0].apply_ready=false;
        currentItems[0].review_state=mode==='source-review'?'source_review':'needs_revalidation';
        currentItems[0].review_notes=[{note:'원문 분석 범위 확인 필요',code:'source_too_long'}];
        currentItems[0].fields.forEach(field=>delete field.snapshot_current);
        if(mode==='unknown-state'){delete currentItems[0].apply_ready;delete currentItems[0].review_state;}
    }
    return {items:currentItems};
};
window.candidateScopes=[];
if(mode==='undo') {
    welfareApi.undoPolicyField=async(id,field)=>{const f=undoFields.find(f=>f.field===field);f.applied=false;delete f.undo_id;return {ok:true};};
    welfareApi.confirmPolicyChange=async()=>{cleaned=true;return {ok:true};};
}
window.dismissedCandidateIds=new Set();
welfareApi.getPolicyCandidates = async dbKeys => {
    window.candidateScopes.push(dbKeys);
    return {items:['draft-candidate','candidate-dismiss'].includes(mode)&&!window.dismissedCandidateIds.has(9)?[{id:9,title:'확인이 필요한 정책 후보',link:'https://example.org/policy/candidate',similar:{db_key:'test'}}]:[]};
};
welfareApi.getPolicyHealth = async () => ({});
window.reviewExclusions=[];
welfareApi.getPolicyReviews=async(keys,offset,signal,excludeIds=[])=>{
    window.reviewExclusions.push(excludeIds);
    return {items:[],total:0,truncated:false};
};
for (const name of ['getPolicyStatus','getPolicySyncStatus','getPolicyAuditStatus','getPolicyRecomputeStatus','getPolicyFullCheckStatus','getPolicyRestyleStatus']) welfareApi[name] = async () => ({running:false});
welfareApi.applyPolicyFieldBulk = async items => {
    window.policyCalls.push(items);
    if(mode==='local-save-bulk')return {applied:0,failed:items.map(item=>({...item,error:'LOCAL_SAVE_FAILED',message:'Notion 반영 후 로컬 저장 상태를 확인하지 못했습니다.'})),skipped:[]};
    if(mode==='uncertain-bulk')return {applied:0,failed:items.map(item=>({...item,error:'UPDATE_UNCERTAIN',message:'Notion 반영 결과를 확인하지 못했습니다.'})),skipped:[]};
    if(mode==='uncertain-bulk-partial')return {applied:1,failed:items.slice(1).map(item=>({...item,error:'UPDATE_UNCERTAIN',message:'Notion 반영 결과를 확인하지 못했습니다.'})),skipped:[]};
    await new Promise(resolve => setTimeout(resolve, 150));
    return {applied:0,failed:items.map(item => ({...item,message:'시험 연결 실패'})),skipped:[]};
};
window.draftCalls={preview:[],create:[],status:[],retry:[],candidate:[],dismiss:[]};
const pendingDraft=[];
const deferDraft=(kind,args)=>{
    window.draftCalls[kind].push(args);
    return new Promise((resolve,reject)=>pendingDraft.push({kind,resolve,reject}));
};
window.finishDraft=(kind,response,fail=false)=>{
    const index=pendingDraft.findIndex(item=>item.kind===kind);
    if(index<0)throw Error(`No pending ${kind}`);
    const [request]=pendingDraft.splice(index,1);
    if(fail)request.reject(response && typeof response === 'object' ? response : Error(response||'시험 요청 응답 끊김'));
    else request.resolve(response);
};
for(const [name,kind] of [['previewPolicyDraftFromUrl','preview'],['createPolicyDraftFromUrl','create'],['checkPolicyDraftStatus','status'],['allowPolicyDraftRetry','retry'],['draftPolicy','candidate']]) welfareApi[name]=async(...args)=>deferDraft(kind,args);
welfareApi.dismissNotionCandidates=async ids=>{
    const response=await deferDraft('dismiss',[ids]);
    ids.forEach(id=>window.dismissedCandidateIds.add(id));
    return response;
};
createRoot(document.getElementById('root')).render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}>
    <div className="flex h-screen"><aside className="w-[260px] shrink-0">시험 메뉴</aside><main className="flex-1 min-w-0 overflow-auto p-8"><PolicyManage/></main></div>
</QueryClientProvider></MemoryRouter>);

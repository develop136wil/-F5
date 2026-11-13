export const QUOTA_MODELS = ['gemini-3.5-flash-lite', 'gemini-embedding-001'];
export const QUOTA_METRICS = [
    {key:'rpm',label:'분당 요청 수 (RPM)',max:10000},
    {key:'tpm',label:'분당 토큰 수 (TPM)',max:10000000},
    {key:'rpd',label:'하루 요청 수 (RPD)',max:1000000},
];

export function quotaDraftFromPolicy(policy) {
    return Object.fromEntries(QUOTA_MODELS.map(model=>[model,Object.fromEntries(QUOTA_METRICS.map(({key,max})=>{
        const value=policy?.state==='current'?policy.observed?.[model]?.[key]:undefined;
        return [key,Number.isInteger(value)&&value>=0&&value<=max?String(value):''];
    }))]));
}

export function quotaObservationPayload(draft,confirmed) {
    if(confirmed!==true)throw Error('현재 프로젝트의 모델별 한도를 직접 확인한 뒤 확인란을 선택해주세요.');
    return {confirmed:true,models:Object.fromEntries(QUOTA_MODELS.map(model=>[model,Object.fromEntries(QUOTA_METRICS.map(({key,label,max})=>{
        const value=String(draft?.[model]?.[key]??'').trim();
        if(!/^\d+$/.test(value)||Number(value)>max)throw Error(`${model}의 ${label}를 0~${max.toLocaleString('ko-KR')} 사이 정수로 입력해주세요.`);
        return [key,Number(value)];
    }))]))};
}

export function quotaPolicyStatusText(policy) {
    if(policy===undefined)return '저장된 한도를 확인 중입니다.';
    if(policy?.state==='current')return `확인한 한도 사용 중 · 확인일 ${policy.observed_on||'확인 필요'} · 7일 유효`;
    if(policy?.state==='reconfirmation_required')return '확인 기간이 지났거나 연결 키가 바뀌었습니다. 프로젝트 한도를 다시 확인해주세요.';
    if(policy?.state==='invalid')return '저장된 한도를 확인하지 못했습니다. 프로젝트 한도를 다시 확인해주세요.';
    return '아직 확인한 프로젝트 한도가 없습니다.';
}

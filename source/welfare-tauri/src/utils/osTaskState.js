export function normalizeOsTaskState(result={}) {
    const known=typeof result.registered==='boolean'&&!result.error;
    return {...result,registered:known?result.registered:null,error:known?'':result.error||'현재 등록 상태를 확인하지 못했습니다.'};
}

export function isConfirmedOsTaskRemoval(result) {
    return result?.status==='success'&&result.registered===false;
}

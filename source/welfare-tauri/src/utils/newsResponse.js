// Successful HTTP responses still need a minimum contract before replacing
// the last known task. Missing data is not proof that a collection finished.
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const id=value=>typeof value==='string'&&value.trim().length>0;
const states=new Set(['ready','running','waiting','complete','failed','cancelled']);
const invalid=()=>{throw new Error('수집 상태 응답을 확인하지 못했습니다. 마지막 상태를 유지하며 다시 확인합니다.');};

export function readNewsWorkflow(value,expectedId){
    if(!object(value)||!id(value.workflow_id)||!states.has(value.state)
        ||(expectedId!==undefined&&value.workflow_id!==expectedId)
        ||(Object.hasOwn(value,'active')&&typeof value.active!=='boolean'))invalid();
    return value;
}
export function readNewsWorkflowHistory(value){
    if(!object(value)||!Array.isArray(value.items))invalid();
    value.items.forEach(item=>readNewsWorkflow(item));
    return value;
}
export function readNewsProfiles(value){
    if(!object(value)||!Array.isArray(value.items)||value.items.some(item=>!object(item)||!id(item.profile_id)))invalid();
    return value;
}
export function readNewsWorkflowOptions(value){
    if(!object(value)||typeof value.enabled!=='boolean')invalid();
    if(value.enabled||value.selection!=null){
        const selection=value.selection;
        if(!object(selection)||!Number.isInteger(selection.revision)||selection.revision<1
            ||!Array.isArray(selection.life_cycles)||!Array.isArray(selection.characteristics))invalid();
    }
    return value;
}
export function readNewsDefaultCondition(value){
    if(!object(value)||!Object.hasOwn(value,'profile')
        ||(value.profile!==null&&(!object(value.profile)||!id(value.profile.profile_id))))invalid();
    return value;
}

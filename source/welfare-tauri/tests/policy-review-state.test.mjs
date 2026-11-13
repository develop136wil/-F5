import test from 'node:test';
import assert from 'node:assert/strict';
import { policyCanApply, policyStateLabel, policyCardReviewNotes, readPolicyChanges, policyWatchOutcome, policyWriteNeedsRecovery } from '../src/utils/policyReviewState.js';
import { selectedVisibleFields, filterPolicyChanges } from '../src/utils/policySelection.js';

test('구제안·보류·불명 응답은 명시적인 새 검증 전에는 반영할 수 없다', () => {
    for (const policy of [{}, {apply_ready:true}, {review_state:'proposal'}, {apply_ready:true,review_state:'source_review'}, {apply_ready:false,review_state:'needs_revalidation'}, {apply_ready:'true',review_state:'proposal'}]) {
        assert.equal(policyCanApply(policy), false);
        assert.deepEqual(selectedVisibleFields([{...policy,id:1,fields:[{field:'분류',new:'복지'}]}],new Set(['1::분류']),true),[]);
    }
    assert.equal(policyCanApply({apply_ready:true,review_state:'proposal'}),true);
    assert.equal(policyCanApply({apply_ready:true,review_state:'proposal',apply_history:[{needs_verification:true}]}),false);
    assert.equal(policyStateLabel({review_state:'source_review'}),'원문 확인 필요');
    assert.equal(policyStateLabel({}),'재점검 필요');
    assert.equal(policyStateLabel({review_state:'completed'}),'반영 완료');
});

test('기본 필터는 사소 제안에 붙은 확인 필요 상태를 숨기지 않는다',()=>{
    const held={id:1,intensity:'low',review_state:'source_review',fields:[]};
    assert.deepEqual(filterPolicyChanges([held],'important'),[held]);
    const notes=policyCardReviewNotes({review_notes:[null,{note:''},{note:'원문 확인',code:'source_too_long'},{note:'중복 설명',code:'source_too_long'}]});
    assert.equal(notes.length,1);assert.match(notes[0].note,/원문이 길어 분석을 완료하지 못했습니다/);assert.equal(notes[0].nextStep,'');
    assert.equal(policyStateLabel(held),'원문 확인 필요');
    assert.equal(policyStateLabel({...held,review_notes:[{code:'source_too_long'}]}),'상세 출처 연결 필요');
    assert.deepEqual(policyCardReviewNotes({review_notes:[{note:'구체적인 실패 사유',code:'unknown'}]}).map(item=>item.note),['구체적인 실패 사유']);
});

test('잘못된 변경 목록은 빈 성공으로 표시하지 않는다',()=>{
    for(const value of [null,{}, {items:null},{items:[{id:1}]}])assert.throws(()=>readPolicyChanges(value));
    assert.deepEqual(readPolicyChanges({items:[]}),{items:[]});
});

test('웹페이지 읽기 실패는 성공 변경 0건으로 표시하지 않는다',()=>{
    assert.equal(policyWatchOutcome({checked:3,changed:0,failed:0,status:'completed'}).level,'success');
    const partial=policyWatchOutcome({checked:2,changed:0,failed:1,status:'partial'});
    assert.equal(partial.level,'warning');assert.match(partial.message,/변경 여부를 확인하지 못했습니다/);
    assert.equal(policyWatchOutcome({checked:1,changed:0,failed:1,status:'error'}).level,'error');
    for(const result of [{checked:3,changed:0},{checked:0,changed:0,failed:1,status:'completed'},{}])assert.throws(()=>policyWatchOutcome(result));
});

test('원격 반영 후 로컬 저장 실패도 후속 반영을 중단한다',()=>{
    for(const error of ['LOCAL_SAVE_FAILED','UPDATE_UNCERTAIN','APPLY_RESULT_UNCERTAIN'])assert.equal(policyWriteNeedsRecovery({error}),true);
    assert.equal(policyWriteNeedsRecovery({reason:'NOT_ATTEMPTED'}),true);
    assert.equal(policyWriteNeedsRecovery({error:'VALIDATION_ERROR'}),false);
});

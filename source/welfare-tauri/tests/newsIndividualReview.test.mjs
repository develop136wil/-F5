import test from 'node:test';
import assert from 'node:assert/strict';
import {workflowStatusText, workflowNotice, canResumeWorkflow} from '../src/utils/newsWorkflowState.js';
import {newsResultPresentation} from '../src/utils/newsResultPresentation.js';

test('개별 보류가 남아도 수집 완료이며 완료하지 못함으로 보이지 않는다',()=>{
    const item={state:'complete',summary:{analysis:{review_required_count:2,completion_status:'complete_with_review'}}};
    assert.equal(workflowStatusText(item),'수집 완료 · 일부 분석 보류');
    assert.match(workflowNotice(item),/2건.*나머지 자료는 처리를 마쳤습니다/);
    assert.equal(canResumeWorkflow(item),false);
});

test('보류 사유는 고정 문구만 표시하고 완료 결과로 가져오지 않는다',()=>{
    for(const reason of ['ai_input_review_required','ai_input_size_review_required','ai_response_review_required']) {
        const view=newsResultPresentation({state:'failed',review_reason:reason});
        assert.match(view.statusLabel,/보류/);
        assert.equal(view.canImport,false);
    }
    assert.equal(newsResultPresentation({state:'failed',review_reason:'secret raw error'}).statusLabel,'분석 미완료');
});

test('OCR 사용 표시는 대상 판단이나 실제 미확인 경고를 바꾸지 않는다',()=>{
    const base={state:'complete',job_id:'job',target_status:'match',import_eligibility:{allowed:true,reason:'confirmation_required'}};
    const clean=newsResultPresentation({...base,source_review:{status:'complete',methods:['local_ocr']}});
    assert.equal(clean.needsSourceReview,false);
    assert.equal(clean.statusLabel,'대상 조건에 맞음 · 이미지 글자 활용');
    const partial=newsResultPresentation({...base,source_review:{status:'partial',methods:['local_ocr']}});
    assert.equal(partial.needsSourceReview,true);
    assert.match(partial.statusLabel,/원문 확인 필요.*이미지 글자 활용/);
});

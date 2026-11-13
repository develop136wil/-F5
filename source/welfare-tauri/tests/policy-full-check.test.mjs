import test from 'node:test';
import assert from 'node:assert/strict';
import {policyFullCheckConfirmation, policyFullCheckPhase, policyFullCheckNeedsAttention, policyFullCheckHasResult} from '../src/utils/policyFullCheck.js';

const plan = {policy_count:5,initial_count:2,pending_count:3,style_count:1,style_configured:true,ai_remaining:0};

test('최초 비교·문장 다듬기 확인은 예상치·이어하기·별도 반영을 안내하고 앱 잔여 한도는 표시하지 않는다', () => {
    const text = policyFullCheckConfirmation(plan);
    for (const expected of ['최초 비교 2건', '저장된 제안 3건', '문장 다듬기 1건', '정확한 AI 호출 횟수가 아닙니다', '같은 버튼', 'Notion에는 자동으로 반영하지 않습니다']) assert.ok(text.includes(expected));
    for(const remaining of [undefined,null,0,20,100]) {
        const confirmation=policyFullCheckConfirmation({...plan,ai_remaining:remaining});
        assert.equal(confirmation,text,'과거 서버의 ai_remaining 값은 확인 내용을 바꾸지 않음');
        assert.doesNotMatch(confirmation,/잔여 한도|한도에 도달|20회/);
    }
    assert.match(policyFullCheckConfirmation({...plan,style_configured:false,style_count:0}), /문장 다듬기는 건너뜁니다/);
});

test('이미 비교한 범위는 바로 시작하고 빈 로컬 범위는 첫 불러오기를 확인한다', () => {
    assert.equal(policyFullCheckConfirmation({...plan,initial_count:0,style_count:0}), null);
    assert.equal(typeof policyFullCheckConfirmation({...plan,policy_count:0,initial_count:0,style_count:0}), 'string');
});

test('조회 응답이 불완전하면 확인 없이 시작하지 않는다', () => {
    for (const value of [null, {}, {...plan,initial_count:-1}, {...plan,style_count:'1'}, {...plan,policy_count:undefined}, {...plan,pending_count:NaN}, {...plan,style_configured:null}]) assert.throws(() => policyFullCheckConfirmation(value), /점검 대상 정보/);
});

test('단계와 최종 상태는 완료·보류·실패를 구분한다', () => {
    assert.equal(policyFullCheckPhase({running:true,phase:'check'}), '최초·변경 내용 비교');
    assert.equal(policyFullCheckPhase({running:true,phase:'embedding'}), '정책 제목 검색 데이터 정리');
    for (const state of ['partial','paused','error']) {
        assert.equal(policyFullCheckNeedsAttention({running:false,state}), true);
        assert.equal(policyFullCheckHasResult({running:false,state}), true);
    }
    assert.equal(policyFullCheckNeedsAttention({running:true,state:'paused'}), false);
    assert.equal(policyFullCheckNeedsAttention({running:false,state:'completed'}), false);
    assert.equal(policyFullCheckHasResult({running:false,state:'idle'}), false);
});

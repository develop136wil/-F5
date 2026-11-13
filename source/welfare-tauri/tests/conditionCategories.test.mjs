import test from 'node:test';
import assert from 'node:assert/strict';
import {conditionCategories,editableCategoryOptions,officialConditionSupport} from '../src/utils/conditionCategories.js';
test('생성·수정의 기본 분류는 같고 일반·기타는 새 선택 항목이 아니다',()=>{
 assert.equal(conditionCategories.life_cycles.includes('기타'),false);
 assert.equal(conditionCategories.characteristics.includes('일반'),false);
 assert.ok(conditionCategories.characteristics.includes('다문화'));
 assert.ok(conditionCategories.characteristics.includes('탈북민'));
});
test('복지로 통합 조회는 허용하되 지원되지 않는 대상만으로 조회하지 않는다',()=>{
 const taxonomy={household_situations:['다문화·탈북민','보훈대상자'],life_cycles:['노년']};
 assert.deepEqual(officialConditionSupport({characteristics:['다문화']},taxonomy),{available:true,supported:true,needsReview:true});
 assert.equal(officialConditionSupport({characteristics:['일반','1인 가구']},taxonomy).supported,false);
 assert.equal(officialConditionSupport({characteristics:['여성'],life_cycles:['노년']},taxonomy).needsReview,true);
 assert.equal(officialConditionSupport({},null).supported,false);
});
test('기존 통합·사용자 분류는 원형을 보존해 수정 화면에 표시한다',()=>{
 const saved=['다문화·탈북민','일반','기관 자체 대상'];
 const result=editableCategoryOptions('characteristics',saved);
 for(const value of saved)assert.ok(result.includes(value));
 assert.deepEqual(saved,['다문화·탈북민','일반','기관 자체 대상']);
 assert.equal(new Set(result).size,result.length);
});

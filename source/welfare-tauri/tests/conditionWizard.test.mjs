import test from 'node:test';
import assert from 'node:assert/strict';
import {validateConditionDraft} from '../src/utils/conditionWizard.js';
const base={region:'전국',min_age_months:'',max_age_months:''};
test('저장 직전에도 지역과 나이를 검증하고 원본은 보존한다',()=>{
 const draft={...base,region:' 도봉구 ',min_age_months:0,max_age_months:108};
 assert.deepEqual(validateConditionDraft(draft),{region:'도봉구',min_age_months:0,max_age_months:108});
 assert.equal(draft.region,' 도봉구 ');
 assert.equal(validateConditionDraft(base).min_age_months,null);
});
test('빈 지역·과도한 길이·제어문자·역전 나이·잘못된 나이는 저장하지 않는다',()=>{
 for(const region of ['', '  ', '가'.repeat(101), '서울\n도봉구', null])
  assert.throws(()=>validateConditionDraft({...base,region}));
 for(const ages of [{min_age_months:109,max_age_months:108},{min_age_months:-1},{max_age_months:1801},{max_age_months:1.5}])
  assert.throws(()=>validateConditionDraft({...base,...ages}));
});

test('세부 설정은 서버의 개수·길이 한도를 지키며 원본을 바꾸지 않는다',()=>{
 const draft={...base,keywords:['복지'],excluded_keywords:['채용'],ai_prompt:'신청 방법을 설명해주세요'};
 const before=structuredClone(draft);validateConditionDraft(draft);assert.deepEqual(draft,before);
 for(const patch of [{keywords:Array(501).fill('단어')},{excluded_keywords:[42]},{ai_prompt:'가'.repeat(10001)}])
  assert.throws(()=>validateConditionDraft({...draft,...patch}));
 validateConditionDraft({...draft,keywords:Array(500).fill('단어'),ai_prompt:'가'.repeat(10000)});
});

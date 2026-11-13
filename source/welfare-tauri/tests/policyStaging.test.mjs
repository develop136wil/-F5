import {test} from 'node:test';
import assert from 'node:assert/strict';
import {selectedVisibleFields} from '../src/utils/policySelection.js';

test('원문 확인 항목도 임시 선택할 수 있지만 일반 일괄 적용 목록에는 섞이지 않는다',()=>{
    const fields=[{field:'추가 자격요건',new:'확인할 조건',requires_individual_review:true},{field:'분류',new:'보육'},{field:'세부 연령',new:''},{field:'거주지',new:'전국',applied:true}];
    const selected=new Set(fields.map(f=>`1::${f.field}`));
    const policy={id:1,fields,apply_ready:true,review_state:'proposal'};
    assert.deepEqual(selectedVisibleFields([policy],selected),[{id:1,field:'분류'}]);
    assert.deepEqual(selectedVisibleFields([policy],selected,true),[{id:1,field:'추가 자격요건'},{id:1,field:'분류'}]);
    assert.deepEqual(selectedVisibleFields([],selected,true),[]);
});

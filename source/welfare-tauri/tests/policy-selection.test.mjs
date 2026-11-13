import test from 'node:test';
import assert from 'node:assert/strict';
import { selectedVisibleFields, allVisibleSelected, isInvalidProposal, filterPolicyChanges, groupPolicyFields } from '../src/utils/policySelection.js';
const ready = {apply_ready:true,review_state:'proposal'};
const rows = [{...ready,id: 1, fields: [{field: '분류', new: '복지'}, {field: '거주지', applied: true}]}];

test('빈값과 잘못된 AI 응답은 선택 및 일괄 반영에서 제외한다', () => {
    for (const value of [null, undefined, '', ' None ', 'NULL', 'undefined', 'NaN', '-', '(없음)', [], {}]) {
        assert.equal(isInvalidProposal(value), true);
        assert.deepEqual(selectedVisibleFields([{...ready,id: 1, fields: [{field: '분류', new: value}]}], new Set(['1::분류'])), []);
    }
    assert.equal(isInvalidProposal('소득 무관'), false);
});
test('일부 반영한 카드는 기본 필터에서 유지하되 명시적 필터는 존중한다', () => {
    const partial = {id: 1, intensity: 'low', fields: [{applied: true}, {applied: false}]};
    const untouched = {id: 2, intensity: 'low', fields: [{applied: false}]};
    assert.deepEqual(filterPolicyChanges([partial, untouched], 'important'), [partial]);
    assert.deepEqual(filterPolicyChanges([partial, untouched], 'high'), []);
    assert.deepEqual(filterPolicyChanges([partial, untouched], 'all'), [partial, untouched]);
});
test('숨겨진 정책과 이미 반영한 내용은 일괄 요청에서 제외한다', () => {
    assert.deepEqual(selectedVisibleFields(rows, new Set(['1::분류', '1::거주지', '2::분류'])), [{id: 1, field: '분류'}]);
});
test('전체 선택 여부는 개수가 아닌 실제 정책 ID로 판단한다', () => {
    assert.equal(allVisibleSelected(rows, new Set([2])), false);
    assert.equal(allVisibleSelected(rows, new Set([1])), true);
    assert.equal(allVisibleSelected([], new Set()), false);
});

test('개별 원문 대조가 필요한 제안은 선택 상태가 남아 있어도 일괄 요청에서 제외한다', () => {
    const changes = [{...ready,id: 1, fields: [{field: '상세 지원 내용', new: '50% 할인', requires_individual_review: true}]}];
    assert.deepEqual(selectedVisibleFields(changes, new Set(['1::상세 지원 내용'])), []);
});

test('유효한 미반영 제안만 세며 사소·미제안·반영 기록을 구분한다', () => {
    const priority = {field:'자격요건',new:'검토할 조건',level:'high',requires_individual_review:true};
    const minor = {field:'지원 내용',new:'표현 정리',level:'low'};
    const styled = {field:'분류',new:'',styled:'의료',level:'info'};
    const missing = {field:'세부 연령',old:'전 연령',new:'',level:'info'};
    const invalid = {field:'소득 수준',new:'None',level:'high'};
    const applied = {field:'거주지',new:'',applied:true};
    const groups = groupPolicyFields([missing,priority,minor,styled,invalid,applied]);
    assert.deepEqual(groups, {priority:[priority,styled],minor:[minor],unavailable:[missing,invalid],applied:[applied],proposalCount:3});
    assert.equal(groupPolicyFields().proposalCount,0);
    const chosen = new Set(['1::세부 연령','1::소득 수준','1::거주지','1::지원 내용']);
    assert.deepEqual(selectedVisibleFields([{...ready,id:1,fields:[missing,minor,invalid,applied]}],chosen,true),[{id:1,field:'지원 내용'}], '접을 수 있는 사소 제안도 선택 후에는 일괄 반영 목록에 남음');
});
test('모두 반영한 사소 변경 카드도 기본 목록에서 직접 정리 전까지 유지한다', () => {
    const card = {id:99,intensity:'low',fields:[{field:'분류',applied:true}]};
    assert.deepEqual(filterPolicyChanges([card], 'important'), [card]);
});

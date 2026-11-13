import { test } from 'node:test';
import assert from 'node:assert/strict';
import { aiUsagePresentation as present } from '../src/utils/aiUsagePresentation.js';

test('조회 전·실패는 사용량 0이나 사용 가능으로 표시하지 않는다', () => {
    assert.equal(present({}).count, null);
    assert.equal(present({isError:true, data:{recorded_requests_today:42}}).count, null);
});
test('앱 기록은 잔여 한도가 아니며 정상 유휴 상태만 표시한다', () => {
    assert.equal(present({data:{recorded_requests_today:42, models:{}}}).count, 42);
    assert.equal(present({data:{models:{}}}).title, 'AI 요청 대기 없음');
});
test('기록 복구와 실제 모델 한도 소진을 구분한다', () => {
    assert.equal(present({data:{state_warning:'복구',recorded_requests_today:0}}).count, null);
    assert.equal(present({data:{state_warning:'복구',models:{'*':{daily_exhausted:true}}}}).title, 'AI 사용 기록 확인 필요');
    assert.match(present({data:{models:{a:{daily_exhausted:true}}}}).title, /일부 AI 모델/);
});
test('현재 대기 요청이 없으면 자동 재시도를 약속하지 않는다', () => {
    const state = present({data:{models:{a:{cooldown_seconds:25, waiting:0}}}});
    assert.match(state.note, /자동으로 시작하지 않습니다/);
});

test('설명 없는 전역 보류도 숨기지 않되 Google 모델 한도 소진으로 단정하지 않는다', () => {
    const state=present({data:{recorded_requests_today:42,local_budget_exhausted:true,models:{'*':{daily_exhausted:true}}}});
    assert.equal(state.title,'AI 사용 상태 확인 필요');
    assert.equal(state.tone,'warning');
    assert.equal(state.count,null);
    assert.match(state.note,/한도 또는 사용 기록.*작업은 보관/);
    assert.doesNotMatch(state.note,/20회|상한에 도달|Google.*소진/);
});
test('대기 시간은 마지막 조회 기준이며 완료 예정 시간이 아니다', () => {
    const state = present({data:{models:{a:{cooldown_seconds:25, waiting:2}}}});
    assert.match(state.note, /2개 요청/);
    assert.match(state.note, /완료 예정 시간은 아닙니다/);
});

test('폐지한 앱 일일 상한 플래그는 경고 없이 무시하고 사용 기록은 보존한다', () => {
    for (const limit of [0,20,100]) {
        const state = present({data:{local_budget_exhausted:true,daily_request_limit:limit,recorded_requests_today:42,models:{}}});
        assert.equal(state.title, 'AI 요청 대기 없음');
        assert.equal(state.tone, 'neutral');
        assert.equal(state.note, '');
        assert.equal(state.count, 42);
    }
});

test('과거 앱 상한 값이 남아도 Google 실제 한도·대기·기록 오류는 숨기지 않는다', () => {
    const base={local_budget_exhausted:true,daily_request_limit:20,recorded_requests_today:42};
    const quota = present({data:{...base,models:{model:{daily_exhausted:true}}}});
    assert.match(quota.title, /일부 AI 모델/);
    assert.match(quota.note, /자동 재개되지는 않습니다/);
    assert.equal(quota.tone,'warning');
    assert.match(present({data:{...base,models:{model:{waiting:2,cooldown_seconds:25}}}}).note,/2개 요청/);
    assert.equal(present({data:{...base,state_warning:'기록 복구 필요'}}).title,'AI 사용 기록 확인 필요');
});

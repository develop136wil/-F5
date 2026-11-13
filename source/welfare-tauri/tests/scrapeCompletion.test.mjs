import test from 'node:test';
import assert from 'node:assert/strict';
import {scrapeCompletionNotice} from '../src/utils/scrapeCompletion.js';

test('분석 실패는 완료 응답에서도 성공으로 알리지 않는다', () => {
    const result = scrapeCompletionNotice({status_code:'completed',save_result:{saved:1},ai_savings:{analysis_failed:1}});
    assert.equal(result.tone,'warning');
    assert.match(result.message,/분석 실패 1건/);
    assert.doesNotMatch(result.message,/1건 저장/);
});
test('보류 또는 일부 사이트 실패 결과는 이유를 유지한다', () => {
    assert.deepEqual(scrapeCompletionNotice({status_code:'partial_failure',message:'원문은 보존했으며 분석 1건을 보류했습니다.'}),
        {tone:'warning',message:'원문은 보존했으며 분석 1건을 보류했습니다.'});
    assert.equal(scrapeCompletionNotice({status_code:'completed',ai_savings:{quota_skipped:2}}).tone,'warning');
});
test('정상 종료, 중지, 실패, 진행 중과 유휴 상태를 구분한다', () => {
    assert.equal(scrapeCompletionNotice({status_code:'completed',save_result:{saved:0}}).tone,'success');
    assert.doesNotMatch(scrapeCompletionNotice({status_code:'completed',save_result:{saved:0}}).message,/찾은 행사|Found|분석 실패/);
    assert.equal(scrapeCompletionNotice({status_code:'stopped'}).tone,'warning');
    assert.equal(scrapeCompletionNotice({status_code:'error',message:'연결 실패'}).tone,'error');
    assert.equal(scrapeCompletionNotice({is_running:true,status_code:'partial_failure'}),null);
    assert.equal(scrapeCompletionNotice({status_code:'idle'}),null);
});

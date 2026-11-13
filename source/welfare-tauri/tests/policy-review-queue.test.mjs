import test from 'node:test';
import assert from 'node:assert/strict';
import { readPolicyReviewQueue, policyReviewSourceUrl, POLICY_REVIEW_PAGE_SIZE } from '../src/utils/policyReviewQueue.js';

const item = {id:'id',status:'unresolved',title:'정책',note:'적용 기준 확인 필요',code:'source_criteria_review_required',source_url:'https://example.org/policy',last_seen_at:'2026-09-13T00:00:00Z'};

test('영속 보류는 제한된 안내와 안전한 출처만 표시한다', () => {
    const result = readPolicyReviewQueue({items:[{...item,raw_source:'never expose',analysis:'never expose'}],total:52,truncated:true});
    assert.equal(POLICY_REVIEW_PAGE_SIZE,50);
    assert.equal(result.total,52);assert.equal(result.hasMore,true);
    assert.match(result.items[0].nextStep,/같은 원문을 반복 점검해도 해결되지 않습니다/);
    assert.deepEqual(Object.keys(result.items[0]),['id','title','note','sourceUrl','lastSeenAt','nextStep']);
});

test('깨진 응답을 정상적인 빈 보류 목록으로 표시하지 않는다', () => {
    for (const value of [null,{}, {items:[],total:0}, {items:[],total:'0',truncated:false},
        {items:[item],total:0,truncated:false}, {items:[{...item,status:'resolved'}],total:1,truncated:false},
        {items:[item,item],total:2,truncated:false}, {items:[null],total:1,truncated:false}]) {
        assert.throws(()=>readPolicyReviewQueue(value),/보류 기록/);
    }
    assert.deepEqual(readPolicyReviewQueue({items:[],total:0,truncated:false}),{items:[],total:0,hasMore:false});
});

test('출처 링크는 스크립트·자격증명·제어문자를 허용하지 않는다', () => {
    for(const url of ['javascript:alert(1)','data:text/html,x','file:///tmp/a','https://user:secret@example.org','https://example.org/\npath','https://','']) assert.equal(policyReviewSourceUrl(url),'');
    assert.equal(policyReviewSourceUrl('https://example.org/공고'),'https://example.org/%EA%B3%B5%EA%B3%A0');
});

test('보류는 길이를 제한하고 미지정 사유에 반복 AI 실행을 권하지 않는다', () => {
    const {items} = readPolicyReviewQueue({items:[{...item,title:'x'.repeat(200),note:'n'.repeat(1000),code:'unknown'}],total:1,truncated:false});
    assert.equal(items[0].title.length,120);assert.equal(items[0].note.length,500);
    assert.match(items[0].nextStep,/자동 반영은 보류/);
});

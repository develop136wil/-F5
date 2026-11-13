import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {newsResultPresentation} from '../src/utils/newsResultPresentation.js';

const ready = () => ({job_id:'job',state:'complete',target_status:'match',
    source_review:{status:'complete'},import_eligibility:{allowed:true,reason:'confirmation_required'}});

test('원문 형식 보류는 입력 분량이나 재시도 대기로 안내하지 않는다', () => {
    const view = newsResultPresentation({...ready(), state:'failed', review_reason:'ai_source_review_required'});
    assert.equal(view.statusLabel, '원문 형식 확인 필요 · 분석 보류');
    assert.equal(view.canImport, false);
    assert.doesNotMatch(view.statusLabel, /분량|한도|재시도/);
});

test('서버가 허용한 완료 결과만 기존 가져오기 확인으로 진행한다', () => {
    for (const target_status of ['match','review']) {
        assert.equal(newsResultPresentation({...ready(),target_status}).canImport,true);
    }
    for (const state of ['queued','running','waiting','failed']) {
        assert.equal(newsResultPresentation({...ready(),state}).canImport,false);
    }
    for (const target_status of ['excluded','legacy','other']) {
        assert.equal(newsResultPresentation({...ready(),target_status}).canImport,false);
    }
});

test('관련 없음이 검토로 남아도 서버 금지 사유를 표시하고 가져오기를 숨긴다', () => {
    const view=newsResultPresentation({...ready(),target_status:'review',
        import_eligibility:{allowed:false,reason:'relevance_unconfirmed'}});
    assert.equal(view.statusLabel,'검토 필요');
    assert.equal(view.canImport,false);
    assert.match(view.importNote,/관련 소식인지 확인되지 않아/);
});

test('없는 메타데이터·문자열 boolean·알 수 없는 코드는 일치 상태만으로 허용하지 않는다', () => {
    for (const import_eligibility of [undefined,null,{},true,{allowed:'true',reason:'confirmation_required'},
        {allowed:true,reason:'unknown'}, {allowed:false,reason:'<script>provider secret</script>'}]) {
        const view=newsResultPresentation({...ready(),import_eligibility});
        assert.equal(view.canImport,false);
        assert.match(view.importNote,/확인하지 못했습니다/);
        assert.doesNotMatch(view.importNote,/script|provider secret/);
    }
});

test('이전 저장 결과도 서버가 재검증해 허용하면 추가 마이그레이션 없이 사용한다', () => {
    const view=newsResultPresentation({...ready(),source_review:{status:'unknown'}});
    assert.equal(view.canImport,true);
    assert.equal(view.statusLabel,'확보한 내용 기준 일치 · 원문 확보 범위 확인 필요');
    assert.equal(view.needsSourceReview,true);
});

test('일부 원문·첨부 미확인은 대상 일치보다 확인 필요를 함께 강조한다', () => {
    const view=newsResultPresentation({...ready(),source_review:{status:'partial'}});
    assert.equal(view.statusLabel,'확보한 내용 기준 일치 · 원문 확인 필요');
    assert.equal(view.needsSourceReview,true);
    assert.equal(view.canImport,true); // still an explicit source-review confirmation
});

test('본문 미확보는 정상 분석처럼 표시하지 않는다', () => {
    for (const target_status of ['match','review']) {
        const view=newsResultPresentation({...ready(),target_status,source_review:{status:'empty'}});
        assert.match(view.statusLabel,/본문을 확보하지 못함/);
        assert.equal(view.needsSourceReview,true);
    }
    assert.equal(newsResultPresentation({...ready(),job_id:null,source_review:{status:'not_applicable'}}).canImport,false);
});

test('원문 대조는 별도 접힘 안내이며 AI 본문이나 HTML로 병합하지 않는다', () => {
    const source=readFileSync(new URL('../src/components/NewsProfileResults.jsx',import.meta.url),'utf8');
    assert.match(source,/원문 대조 안내/);
    assert.match(source,/자동 반영하지 않으며 원문 전체를 확인/);
    assert.match(source,/evidence\.omitted_count > 0/);
    assert.match(source,/\{passage.text\}/);
    assert.doesNotMatch(source,/dangerouslySetInnerHTML|innerHTML\s*=|\.\.\.evidence|Object.assign/);
});

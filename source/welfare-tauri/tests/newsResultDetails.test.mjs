import test from 'node:test';
import assert from 'node:assert/strict';
import {checkedDetailResponse, detailEntries, detailOmissions, fitsDetailLimit, storedNewsPresentation} from '../src/utils/newsResultDetails.js';
import {newsResultPresentation} from '../src/utils/newsResultPresentation.js';

test('전체 상세는 허용 필드의 30000자와 꼬리 조건을 그대로 유지한다', () => {
    for (const length of [4001,16001,30000]) {
        const text='가'.repeat(length-10)+'끝부분조건확인필요';
        const response={record_id:'job:a',ai_called:false,details:{support_detail:text}};
        assert.equal(checkedDetailResponse(response,'job:a'),response);
        assert.equal(detailEntries(response.details)[0].text,text);
    }
});
test('다른 행·과대값·비허용 필드·잘못된 반환은 묵시적 표시하지 않는다', () => {
    const base={record_id:'job:a',ai_called:false,details:{support_detail:'내용'}};
    for (const response of [{...base,record_id:'job:b'},{...base,ai_called:true},
        {...base,details:{support_detail:'가'.repeat(30001)}},{...base,details:{raw_response:'private'}},
        {...base,details:[]},null]) assert.throws(()=>checkedDetailResponse(response,'job:a'));
});
test('서버의 Unicode code point 한도와 이모지·혼합 문자열 한도가 같다', () => {
    for (const text of ['😀'.repeat(30000), '😀가'.repeat(15000)]) {
        assert.equal(fitsDetailLimit(text),true);
        assert.equal(detailEntries({support_detail:text})[0].text,text);
        assert.equal(checkedDetailResponse({record_id:'a',ai_called:false,details:{support_detail:text}},'a').details.support_detail,text);
        assert.equal(fitsDetailLimit(text+'끝'),false);
    }
});

test('표시용 내용·출처 안내를 검증하고 원문 필드는 표시용 객체에 섞지 않는다', () => {
    const base={record_id:'job:a',ai_called:false,details:{support_detail:'정리한 내용'}};
    for (const kind of ['source_excerpt','structured','legacy']) {
        const result={...base,presentation_kind:kind,source_note:'😀'.repeat(1000)};
        assert.equal(checkedDetailResponse(result,'job:a'),result);
    }
    for (const extra of [{presentation_kind:'raw'},{presentation_kind:null},{source_note:{}},
        {source_note:'가'.repeat(1001)}]) {
        assert.throws(()=>checkedDetailResponse({...base,...extra},'job:a'));
    }
    const presentation={support_detail:'',presentation_kind:'source_excerpt',source_note:'원문 확인 필요',source_text:'숨길 원문'};
    assert.deepEqual(storedNewsPresentation({news_presentation:presentation,ai_data:{support_detail:'원문을 되살리면 안 됨'}}),
        {support_detail:'',presentation_kind:'source_excerpt',source_note:'원문 확인 필요'});
    assert.equal(storedNewsPresentation({support_detail:'기존 내용'}),null);
    for (const invalid of [null,[],{support_detail:42},{support_detail:'가'.repeat(30001)},
        {support_detail:'',source_note:'가'.repeat(1001)},{support_detail:'',presentation_kind:'raw'}]) {
        const result=storedNewsPresentation({news_presentation:invalid,ai_data:{support_detail:'원문을 되살리면 안 됨'}});
        assert.equal(result.support_detail,'');
        assert.match(result.source_note,/원문을 확인/);
    }
});
test('생략 메타데이터는 고정 제목·검증된 길이만 표시하며 raw 입력을 합치지 않는다', () => {
    const result=detailOmissions({support_detail:{length:4500,available:true,key:'raw',label:'가짜',text:'private'},
        raw_source:{length:9000,available:true},contact:{length:'10',available:true}});
    assert.deepEqual(result,[{key:'support_detail',label:'지원 내용',length:4500,reason:undefined,available:true}]);
    assert.deepEqual(detailEntries({source_body:'private',support_detail:42}),[]);
});
test('길이 제한 보류는 재분석이나 새로고침을 권하지 않고 기록 보존을 알린다', () => {
    const result=newsResultPresentation({state:'complete',job_id:'a',target_status:'review',
        import_eligibility:{allowed:false,reason:'import_text_too_long'}});
    assert.equal(result.canImport,false);
    assert.equal(result.importNote,'본문이 길어 새 소식 후보로 가져오기를 보류했습니다. 분석 기록과 원문은 유지됩니다.');
    assert.doesNotMatch(result.importNote,/새로고침|재분석/);
});
test('저장 분석 검증 실패는 단순 새로고침으로 해결된다고 안내하지 않는다', () => {
    const result=newsResultPresentation({state:'complete',job_id:'a',target_status:'review',
        import_eligibility:{allowed:false,reason:'invalid_saved_result'}});
    assert.equal(result.canImport,false);
    assert.equal(result.importNote,'저장된 분석의 근거를 확인하지 못해 가져오기를 보류했습니다. 원문과 분석 기록을 확인해주세요.');
});

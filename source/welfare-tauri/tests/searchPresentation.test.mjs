import test from 'node:test';
import assert from 'node:assert/strict';
import {searchPreviewText} from '../src/utils/searchPresentation.js';

const raw = '--- [첨부파일: 본문 이미지] --- 메뉴 COPYRIGHT 숨겨야 하는 원문';
test('검색 카드는 정리한 내용만 평문으로 표시하고 숫자와 조건은 보존한다', () => {
    const item = {support_detail:raw, news_presentation:{presentation_kind:'structured',
        support_detail:'### 지원 내용\n- **교재비 5만원(50,000원)** 지원.\n- 중복 신청은 불가합니다.\n### 신청 방법\n[신청](https://example.invalid)'}};
    assert.equal(searchPreviewText(item), '지원 내용 교재비 5만원(50,000원) 지원. 중복 신청은 불가합니다. 신청 방법 신청');
    assert.equal(item.support_detail, raw);
});
test('빈값·잘못된값·누락된 표시값에서 원문이나 과거 AI 내용을 복원하지 않는다', () => {
    for (const projection of [undefined, null, [], {support_detail:''},
        {support_detail:'',source_note:'원문을 확인해주세요.'}, {support_detail:42},
        {support_detail:'',presentation_kind:'raw'}, {support_detail:'가'.repeat(30001)}]) {
        const result = searchPreviewText({support_detail:raw,ai_data:{support_detail:raw}, news_presentation:projection});
        assert.doesNotMatch(result,/COPYRIGHT|첨부파일|숨겨야/);
        assert.match(result,/확인/);
    }
});
test('정책 검색 본문은 뉴스 표시값 유무와 무관하게 보존한다', () => {
    const policy = {item_type:'policy',support_detail:'### 담당자가 관리한 정책\n지원 금액 **10만원**',
        news_presentation:{support_detail:''}};
    assert.equal(searchPreviewText(policy),policy.support_detail);
    for (const ai of [{item_type:'policy'},JSON.stringify({item_type:'policy'})]) {
        const stored={support_detail:policy.support_detail,ai_data:ai,news_presentation:{support_detail:''}};
        assert.equal(searchPreviewText(stored),policy.support_detail);
    }
    assert.equal(searchPreviewText({item_type:'policy',target:'대상 안내'}),'대상 안내');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(
    new URL('../src/components/ItemDetailModal.jsx', import.meta.url), 'utf8');

test('상세 조회가 성공한 뒤 관련 항목을 이어서 불러옴', () => {
    const detailCall = source.indexOf('welfareApi.getItemDetail(itemId)');
    const detailSuccess = source.indexOf('.then(data =>', detailCall);
    const similarCall = source.indexOf('welfareApi.getSimilarItems(itemId, 5)', detailSuccess);
    const detailFailure = source.indexOf('.catch(err =>', detailSuccess);
    assert.ok(detailCall >= 0 && detailSuccess > detailCall);
    assert.ok(similarCall > detailSuccess && similarCall < detailFailure,
        '관련 항목 요청은 최초 loading 상태가 아니라 상세 조회 성공 뒤 시작해야 합니다');
});

test('상세창은 키보드 닫기와 대화상자 의미를 제공함', () => {
    assert.match(source, /role="dialog"/);
    assert.match(source, /aria-modal="true"/);
    assert.match(source, /event\.key === 'Escape'/);
    assert.match(source, /상세 정보 닫기/);
});

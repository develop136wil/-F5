import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const utilUrl = new URL('../src/utils/searchMatch.js', import.meta.url);
const pageUrl = new URL('../src/pages/Search.jsx', import.meta.url);

test('혼합 검색 결과는 사용자가 이해할 수 있는 일치 이유를 표시함', async () => {
    const { describeSearchMatch } = await import(utilUrl.href);
    assert.equal(describeSearchMatch({ match_sources: ['keyword', 'semantic'] }, true).label, '검색어·의미 모두 일치');
    assert.equal(describeSearchMatch({ match_sources: ['keyword', 'semantic'], keyword_match: 'partial_terms' }, true).label, '핵심 단어·비슷한 뜻');
    assert.equal(describeSearchMatch({ match_sources: ['keyword'], keyword_match: 'all_terms' }, true).label, '검색어 일치');
    assert.equal(describeSearchMatch({ match_sources: ['keyword'], keyword_match: 'partial_terms' }, true).label, '핵심 단어 일부 일치');
    assert.equal(describeSearchMatch({ match_sources: ['semantic'] }, true).label, '비슷한 뜻');
    assert.equal(describeSearchMatch({ match_sources: ['semantic'] }, false), null);
});

test('혼합 순위 내부값을 오해하기 쉬운 관련도 퍼센트로 노출하지 않음', async () => {
    const source = await readFile(pageUrl, 'utf8');
    assert.doesNotMatch(source, /관련도\s*\{\(item\.score\s*\*\s*100\)/);
    assert.match(source, /describeSearchMatch/);
});

test('처음 사용자에게 자연어 통합 검색을 기본 제공함', async () => {
    const source = await readFile(pageUrl, 'utf8');
    assert.match(source, /useSearchSessionField\('semanticMode'\)/);
    const defaults = await readFile(new URL('../src/utils/searchSession.js', import.meta.url), 'utf8');
    assert.match(defaults, /semanticMode: true/);
    assert.match(source, /통합 검색 \(추천\)/);
    assert.match(source, /정확한 단어만/);
});

test('통합 검색의 전체 후보 수와 실제 표시 수를 구분함', async () => {
    const source = await readFile(pageUrl, 'utf8');
    assert.match(source, /visibleResult = searchEnabled && !isPlaceholderData \? searchResult : undefined/);
    assert.match(source, /visibleResult\?\.total \?\? results\.length/);
    assert.match(source, /총[\s\S]*건 중 관련도가 높은[\s\S]*results\.length/);
});

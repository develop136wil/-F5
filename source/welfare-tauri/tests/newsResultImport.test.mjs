import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=p=>readFileSync(new URL(p,import.meta.url),'utf8');

test('이전 가져오기 호환 컴포넌트는 준비 조회와 명시적 확인 후에만 쓰기 요청한다',()=>{
    const source=read('../src/components/NewsResultImport.jsx');
    assert.match(source,/previewNewsResultImport\(profileId,jobId\)/);
    assert.match(source,/if\(!confirmed \|\| !current\)return/);
    assert.match(source,/importNewsResult\(profileId,jobId,current.fingerprint\)/);
    assert.match(source,/if\(pending.current/);
    assert.doesNotMatch(source,/uploadToNotion|startNewsWorkflow|dangerouslySetInnerHTML/);
});
test('처리 기록은 읽기 전용으로 유지하고 최종 저장은 찾은 소식에서 진행한다',()=>{
    const source=read('../src/components/NewsProfileResults.jsx');
    assert.match(source,/newsResultPresentation\(item\)/);
    assert.match(source,/isRealLink\(item.link\)/);
    assert.match(source,/<NewsResultDetails/);
    assert.match(source,/\/notion\?profile=\$\{encodeURIComponent\(profileId\)\}/);
    assert.match(source,/찾은 소식으로 이동/);
    assert.doesNotMatch(source,/NewsResultImport|importNewsResult\(|uploadToNotion\(/);
    assert.match(source,/별도로 가져오는 단계는 없습니다/);
});
test('이전 가져오기 호환 API는 입력 사실을 재전송하지 않고 후보·검색 캐시를 갱신한다',()=>{
    const api=read('../src/api/welfare.js');
    assert.match(api,/\/import`, \{fingerprint, confirmed: true\}/);
    const source=read('../src/components/NewsResultImport.jsx');
    assert.match(source,/queryKeys.notionCandidates,queryKeys.summaryStats,\['search'\]/);
    assert.match(source,/already_imported/);
    assert.match(source,/같은 원문을 중복으로 추가하지 않습니다/);
});

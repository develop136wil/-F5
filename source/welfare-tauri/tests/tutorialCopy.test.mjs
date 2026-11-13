import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const tutorial = read('../src/pages/Tutorial.jsx');

test('기능별 도움말은 모든 기능을 필수 초기 설정 순서처럼 번호 매기지 않는다', () => {
    assert.match(tutorial, /기능별 사용 방법/);
    assert.doesNotMatch(tutorial, /title:\s*'\d+\./);
    assert.match(tutorial, /새 소식 수집과는 별도 기능/);
    assert.match(tutorial, />설정할 기능을 선택하세요\.<\/p>/);
    assert.doesNotMatch(tutorial, /먼저 설정할 기능|두 기능 모두 설정할 수 있습니다|Notion API 등록은 공통입니다/);
});

test('새 소식 기본 조건과 별도 조건의 출처 범위를 구분한다', () => {
    assert.match(tutorial, /기본 조건은 등록된 모든 기관 사이트/);
    assert.match(tutorial, /다른 조건은 그 조건에 선택한 사이트·정보 출처/);
    assert.doesNotMatch(tutorial, /등록된 모든 사이트를 자동 수집/);
    assert.match(tutorial, /현재 조건과 예약은 바뀌지 않습니다/);
});

test('새 소식은 기관 게시판과 게시 기간을 안내하고 과거 복지로 조회를 요구하지 않는다', () => {
    assert.match(tutorial, /기관 사이트에서 새 소식을 찾으려면/);
    assert.match(tutorial, /대표 홈페이지 대신 공지사항·모집 게시글 목록 화면/);
    assert.match(tutorial, /기본 게시 기간은 최근 30일/);
    assert.match(tutorial, /수집일·마감일이 아닌 게시일 기준/);
    assert.doesNotMatch(tutorial, /별도 조건에서 복지로만 선택|공식 서비스 결과는 새 게시물/);
});

test('새 조건 결과는 찾은 소식 하나에서 확인하고 중간 가져오기 없이 최종 저장한다', () => {
    const runPanel = read('../src/components/NewsProfileRunPanel.jsx');
    const page = read('../src/pages/NotionUpload.jsx');
    assert.match(tutorial, /관련 결과는 "찾은 소식" 하나에 모입니다/);
    assert.match(tutorial, /제목을 눌러 대상과 지원 내용을 상세 창에서 확인/);
    assert.match(tutorial, /필요한 항목만 선택해 소식 모음 파일로 저장하거나 Notion에 등록/);
    assert.match(tutorial, /AI 분석은 지원 자격의 확정 판정이 아닙니다/);
    assert.match(tutorial, /소식 모음 파일 저장은 Notion 없이 가능/);
    assert.match(runPanel, /찾은 소식 보기/);
    assert.match(page, /소식 모음 파일 저장/);
    assert.match(page, /Notion에 등록/);
    assert.doesNotMatch(tutorial + page, /새 소식 후보로 가져오기/);
});

test('새 기본 조건에 적용되지 않는 예전 고급 설정을 정확히 구분한다', () => {
    assert.match(tutorial, /찾을 단어·제외할 단어·AI 요청은 각 조건의 "수집 세부 설정"/);
    assert.doesNotMatch(tutorial, /이전 수집 방식의 고급 설정/);
    const settings = read('../src/pages/Settings.jsx');
    assert.doesNotMatch(settings, /이전 수집 설정|CUSTOM_AI_PROMPT|welfareApi\.getKeywords/);
});

test('사이트 규칙 분석 생략을 이후 모든 본문 AI 분석 생략으로 설명하지 않는다', () => {
    const sites = read('../src/pages/Sites.jsx');
    assert.match(sites, /수집 확인 필요/);
    assert.match(sites, /주소 등록/);
    assert.match(sites, /!batchStatus\?\.is_running && failedRegistrationUrls.length > 0/);
    assert.doesNotMatch(sites, /실패한 주소만 저장|분석 없이 등록/);
    assert.match(sites, /기본 링크 탐색/);
    assert.match(sites, /수집 다시 확인/);
    assert.doesNotMatch(sites, /AI 분류는 건너뜁니다|단순 수집 \(AI 분석 생략\)|제목과 링크만 수집합니다/);
    // The current profile collector fetches bodies even for fallback-link sites.
    const collector = read('../../welfare_scraper/services/news_site_collector.py');
    // Bounded readers still fetch each candidate's body; fallback registration
    // is not a title-only mode. Runtime behaviour is covered by collector tests.
    assert.match(collector, /page = await fetcher\(session, candidate\['link'\]\)/);
    assert.match(collector, /_read_candidate\(session, candidate, content_fetcher, recover,\s*publication_window\)/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const panel = readFileSync(new URL('../src/components/NewsCollectionPanel.jsx', import.meta.url), 'utf8');

test('수집 조건을 제목 아래 한 번 표시하고 실행 제목 중복을 제거한다', () => {
    assert.match(panel, /id="news-collection-title"[^>]*>수집 범위/);
    assert.doesNotMatch(panel, /영유아·아동 중심/);
    assert.match(panel, /<dl className="grid grid-cols-/);
    assert.match(panel, /config\.data\.TARGET_REGION \|\| '지역 미설정'/);
    assert.match(panel, /config\.data\.TARGET_AGE_MAX_MONTHS == null \? '나이 미설정'/);
    assert.doesNotMatch(panel, /새 게시물 찾기|현재 수집:/);
    const page = readFileSync(new URL('../src/pages/NotionUpload.jsx', import.meta.url), 'utf8');
    assert.match(page, /등록한 기관 게시판에서 소식을 찾아 한곳에 모읍니다/);
    assert.match(page, /소식을 눌러 확인한 뒤, 필요한 항목을 선택해 저장하세요\./);
    assert.doesNotMatch(panel, /정책 최신화·Notion 저장은 별도로 실행합니다/);
});

test('기본 조건 유무와 관계없이 기존 수집 범위 배치와 직접 등록 동선을 유지한다', () => {
    assert.equal((panel.match(/to="\/sites"/g) || []).length, 2);
    assert.equal((panel.match(/수집 범위·기간 설정/g) || []).length, 2);
    assert.match(panel, /공지사항·모집 게시판을 먼저 등록해주세요\./);
    assert.match(panel, /bg-teal-700[^\n]*sm:w-1\/2 sm:flex-none/);
    assert.doesNotMatch(panel, /bg-indigo-600|영유아·아동/);
    assert.ok(panel.indexOf('<dl className=') < panel.indexOf('onClick={()=>setupFirst?'));
    assert.ok(panel.indexOf('border-t border-gray-100 pt-4') < panel.indexOf('onClick={()=>setupFirst?'));
});

test('명시적 시작과 중지 및 실행 전 안전 확인은 유지한다', () => {
    assert.match(panel, /onClick=\{\(\)=>setupFirst\?navigate\('\/settings\?tab=keywords'\):startScrape\(\)\} disabled=\{Boolean\(reason\)\}/);
    assert.match(panel, /legacy_collection_available!==true/);
    assert.match(panel, /needsSetup&&!isScraping&&!isStarting/);
    assert.match(panel, /onClick=\{stopScrape\} disabled=\{isStopping\}/);
    assert.match(panel, /config\.isError \|\| sites\.isError \|\| ai\.isError/);
    assert.match(panel, /ai\.data\?\.busy/);
    assert.match(panel, /actionBusy \?/);
    assert.match(panel, /config\.isError \? '수집 조건을 확인하지 못했습니다.'/);
});

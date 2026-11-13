import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
test('한국어 제품명과 한국어 설치 언어를 사용하되 앱 식별자는 보존한다',()=>{
    const config=JSON.parse(read('../src-tauri/tauri.conf.json'));
    assert.equal(config.productName,'복지F5');
    assert.equal(config.identifier,'com.searchmaster.welfare');
    assert.deepEqual(config.bundle.windows.nsis.languages,['Korean']);
    assert.equal(config.bundle.windows.nsis.displayLanguageSelector,false);
    assert.equal(config.bundle.windows.nsis.template,'./installer-template.nsi');
});
test('표시명 변경에도 기존 설치 등록키와 설치 경로 조회를 보존한다',()=>{
    const template=read('../src-tauri/installer-template.nsi');
    assert.ok(template.includes('Uninstall\\welfare-scraper"'));
    assert.ok(template.includes('!define MANUPRODUCTKEY "${MANUKEY}\\welfare-scraper"'));
    assert.ok(template.includes('ReadRegStr $4 SHCTX "${MANUPRODUCTKEY}" ""'));
    assert.ok(template.includes('WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"'));
    assert.ok(template.includes('!insertmacro NSIS_HOOK_PREINSTALL'));
    assert.ok(read('../src-tauri/installer-hooks.nsh').includes('IsShortcutTarget "${ROOT}\\welfare-scraper.lnk"'));
});
test('설치 화면은 개발 용어 대신 사용자가 선택할 행동을 설명한다',()=>{
    const template=read('../src-tauri/installer-template.nsi');
    const hooks=read('../src-tauri/installer-hooks.nsh');
    for(const phrase of [
        '현재 버전 다시 설치',
        '복지F5 삭제',
        '현재 설정을 유지하고 새 버전 설치',
        '저장된 설정과 데이터도 함께 삭제',
        '복지F5 설치 도우미',
    ]) assert.ok(template.includes(phrase),`missing friendly installer phrase: ${phrase}`);
    for(const jargon of ['컴포넌트 추가 및 재설치','수행하려는 관리 옵션','WebView2 부트스트래퍼','그래픽 인터페이스 설치 프로그램','애플리케이션 데이터 삭제하기']){
        assert.ok(!template.includes(`"${jargon}`),`developer-facing installer phrase remains: ${jargon}`);
    }
    assert.ok(hooks.includes('복지F5가 종료됐는지 확인하지 못했습니다.'));
    assert.ok(hooks.includes('복지F5가 사용 중인 파일을 닫지 못했습니다.'));
    assert.ok(!template.includes('{{#each language_files}}'));
});

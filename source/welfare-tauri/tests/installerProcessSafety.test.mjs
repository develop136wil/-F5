// Read-only static source contracts. No compiler, app, installer, registry,
// process enumeration, network, environment configuration or DB access.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read = name => readFileSync(new URL(`../src-tauri/${name}`, import.meta.url), 'utf8');
const template = read('installer-template.nsi');
const hooks = read('installer-hooks.nsh');
const closeGuard = hooks.match(/!macro WELFARE_REQUIRE_CLOSED\r?\n([\s\S]*?)!macroend/)[1];
const section = name => template.match(new RegExp(`^Section ${name}\\r?\\n([\\s\\S]*?)^SectionEnd`, 'm'))[1];

test('설치와 제거는 필수 종료 거절 hook 없이 컴파일할 수 없다', () => {
    for (const [name, hook, operation] of [
        ['Install', 'NSIS_HOOK_PREINSTALL', 'SetOutPath $INSTDIR'],
        ['Uninstall', 'NSIS_HOOK_PREUNINSTALL', 'Delete "$INSTDIR\\${MAINBINARYNAME}.exe"'],
    ]) {
        const body = section(name);
        const branch = body.match(new RegExp(`!ifmacrodef ${hook}\\s+!insertmacro ${hook}\\s+!else\\s+!error "[^"\\r\\n]+"\\s+!endif`));
        assert.ok(branch, `${name}: missing hook must be a compiler error`);
        assert.ok(body.indexOf(operation) > branch.index + branch[0].length,
            `${name}: local file operation must follow the required guard`);
        const definition = hooks.match(new RegExp(`!macro ${hook}\\s+!insertmacro WELFARE_REQUIRE_CLOSED\\s+!macroend`));
        assert.ok(definition, `${hook}: must use the reviewed common guard`);
    }
});

test('제품 설치 경로는 실행파일 이름으로 프로세스를 검색하거나 종료하지 않는다', () => {
    const dangerousInvocation = /^\s*(?:!insertmacro\s+CheckIfAppIsRunning\b|nsis_tauri_utils::(?:FindProcess|KillProcess)(?:CurrentUser)?\b|(?:Exec|ExecWait|nsExec::Exec)\b[^\r\n]*(?:taskkill|Stop-Process))/mi;
    assert.doesNotMatch(template, dangerousInvocation);
    assert.doesNotMatch(hooks, dangerousInvocation);
    // The stock utils include is still needed by shortcut macros; its unused
    // process macro definition must not be invoked by this product template.
    assert.ok(template.includes('!include "utils.nsh"'));
    assert.equal((template.match(/!insertmacro CheckIfAppIsRunning/g) || []).length, 0);
});

test('종료 거절은 mutex와 정확한 대상 EXE 두 개의 배타적 읽기 검사만 사용한다', () => {
    assert.ok(closeGuard.includes('OpenMutexW(i 0x100000, i 0, w "Local\\SearchMaster.WelfareF5.Desktop")'));
    const calls = [...closeGuard.matchAll(/System::Call 'kernel32::CreateFileW\(w "([^"]+)", i 0x80000000, i 0, p 0, i 3, i 0, p 0\) p\.r0 \?e'/g)];
    assert.deepEqual(calls.map(match => match[1]), [
        '$INSTDIR\\${MAINBINARYNAME}.exe', '$INSTDIR\\api-server.exe',
    ]);
    assert.equal((closeGuard.match(/CreateFileW/g) || []).length, 2);
    for (const call of calls) {
        const tail = closeGuard.slice(call.index + call[0].length);
        assert.match(tail, /^\s+Pop \$1\s+\$\{If\} \$0 == -1\s+\$\{If\} \$1 != 2\s+\$\{AndIf\} \$1 != 3\s+MessageBox [^\r\n]+\s+Abort\s+\$\{EndIf\}\s+\$\{Else\}\s+System::Call 'kernel32::CloseHandle\(p r0\)'\s+\$\{EndIf\}/);
    }
    assert.doesNotMatch(closeGuard, /(?:KillProcess|TerminateProcess|taskkill|Stop-Process|FindProcess)/i);
    assert.match(closeGuard, /Pop \$1\s+Pop \$0\s*$/);
});

test('검증되지 않은 이전 파일명은 삭제하지 않고 안내하며 바로가기 비교값은 유지한다', () => {
    const legacy = template.match(/ReadRegStr \$OldMainBinaryName[^\r\n]+\r?\n([\s\S]*?); Save current MAINBINARYNAME/)[0];
    assert.match(legacy, /DetailPrint "이전 실행 파일 이름을 안전하게 확인할 수 없어 해당 파일을 보존했습니다\."/);
    assert.doesNotMatch(legacy, /^\s*(?:Delete|RMDir|Rename|CopyFiles|Exec|ExecWait)\b/mi);
    assert.doesNotMatch(template, /^\s*(?:Delete|RMDir|Rename|CopyFiles|Exec|ExecWait)\b[^\r\n]*\$OldMainBinaryName/mi);
    assert.doesNotMatch(template, /^\s*StrCpy\s+\$\w+[^\r\n]*\$OldMainBinaryName/mi);
    assert.equal((template.match(/!insertmacro IsShortcutTarget[^\r\n]*\$OldMainBinaryName/g) || []).length, 3);
    assert.ok(template.includes('WriteRegStr SHCTX "${UNINSTKEY}" "MainBinaryName" "${MAINBINARYNAME}.exe"'));
});

test('경로·와일드카드·DB를 담은 레지스트리 값에도 삭제 지시가 없다 (정적 계약)', () => {
    const legacy = template.match(/ReadRegStr \$OldMainBinaryName[^\r\n]+\r?\n([\s\S]*?); Save current MAINBINARYNAME/)[1];
    // These substitutions inspect the only legacy-name branch. They do not
    // execute NSIS or claim a Windows registry/install integration test.
    for (const value of ['..\\welfare.db', '../config.json', '*', '*.db',
        'welfare.db', 'config.json', '.env', 'C:\\outside\\file.txt',
        '\\\\server\\share\\file.txt', 'old-app.exe', '복지F5.exe', '']) {
        const branch = legacy.replaceAll('$OldMainBinaryName', value);
        assert.doesNotMatch(branch, /^\s*(?:Delete|RMDir|Rename|CopyFiles|Exec|ExecWait)\b/mi, value);
        assert.ok(branch.includes('해당 파일을 보존했습니다.'), value);
    }
});

test('기존 명시적 데이터 삭제 동의와 업데이트·무인 보존 조건을 바꾸지 않는다', () => {
    const dataGuard = hooks.match(/!macro WELFARE_DELETE_USER_DATA_IF_REQUESTED BASE\r?\n([\s\S]*?)!macroend/)[1];
    for (const token of ['$DeleteAppDataCheckboxState = 1', '$UpdateMode <> 1',
        '$PassiveMode <> 1', '${AndIfNot} ${Silent}']) assert.ok(dataGuard.includes(token), token);
    assert.match(template, /Function un\.onInit\s+;[^\n]*\n\s+StrCpy \$DeleteAppDataCheckboxState 0/);
});

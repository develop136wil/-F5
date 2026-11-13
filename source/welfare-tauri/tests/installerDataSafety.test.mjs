import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, mkdtempSync, mkdirSync, writeFileSync, existsSync, symlinkSync, unlinkSync, rmSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const hooksPath = fileURLToPath(new URL('../src-tauri/installer-hooks.nsh', import.meta.url));
const hooks = readFileSync(hooksPath, 'utf8');
const template = readFileSync(new URL('../src-tauri/installer-template.nsi', import.meta.url), 'utf8');
const dataMacro = hooks.match(/!macro WELFARE_DELETE_USER_DATA BASE\r?\n([\s\S]*?)!macroend/)[1];

test('실제 데이터 경로는 명시적 사용자 선택에서만 정리한다', () => {
    assert.match(template, /Function un\.onInit\s+;[^\n]*\n\s+StrCpy \$DeleteAppDataCheckboxState 0/);
    const guard = hooks.match(/!macro WELFARE_DELETE_USER_DATA_IF_REQUESTED BASE\r?\n([\s\S]*?)!macroend/)[1];
    for (const phrase of ['$DeleteAppDataCheckboxState = 1', '$UpdateMode <> 1', '$PassiveMode <> 1', '${AndIfNot} ${Silent}']) {
        assert.ok(guard.includes(phrase));
    }
    assert.ok(template.includes('!insertmacro WELFARE_DELETE_USER_DATA_IF_REQUESTED "$LOCALAPPDATA"'));
    assert.ok(template.includes('${AndIfNot} ${Silent}\n') || template.includes('${AndIfNot} ${Silent}\r\n'));
});

test('설치 파일 공유 폴더는 재귀 삭제하지 않고 연결된 폴더를 따라가지 않는다', () => {
    assert.ok(dataMacro.includes('GetFullPathName $1 "$0\\welfare-scraper"'));
    assert.ok(dataMacro.includes('${GetParent} "$1" $2'));
    assert.ok(dataMacro.includes('${AndIf} $2 == $0'));
    assert.equal((dataMacro.match(/IntOp \$4 \$3 & 0x410/g) || []).length, 4);
    assert.doesNotMatch(dataMacro, /RMDir\s+\/r\b/i);
    assert.doesNotMatch(dataMacro, /Delete[^\n]*(?:\.exe|\\\*"|\\\*\.db")/i);
    for (const filename of ['.env', 'config.json', 'config.json.bak', 'welfare.db', 'welfare.db-wal', 'welfare.db-shm', 'attachment-text-cache.sqlite3', 'ai_quota_state.json', 'ai_quota_state.json.minute.json', 'ai_quota_state.json.lock']) {
        assert.ok(dataMacro.includes(`Delete "$1\\${filename}"`), filename);
    }
});

// Opt-in integration check. Builds only a disposable harness; never launches an
// operational uninstaller, reads the real profile, or touches the registry.
test('NSIS 데이터 삭제 매크로는 임시 프로필에서 선택/업데이트/연결 폴더를 안전하게 처리한다', {
    skip: process.platform !== 'win32' || !process.env.NSIS_COMPILER,
}, () => {
    const fixtureBase = fileURLToPath(new URL('../../artifacts', import.meta.url));
    mkdirSync(fixtureBase, {recursive: true});
    const temporary = mkdtempSync(path.join(fixtureBase, 'nsis-data-safety-'));
    assert.equal(path.dirname(temporary), fixtureBase);
    const junctions = [];
    try {
        const exe = path.join(temporary, 'cleanup-harness.exe');
        const script = path.join(temporary, 'cleanup-harness.nsi');
        writeFileSync(script, `Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!include "${hooksPath}"
OutFile "${exe}"
RequestExecutionLevel user
SilentInstall silent
Var DeleteAppDataCheckboxState
Var UpdateMode
Var PassiveMode
Section
  \${GetOptions} $CMDLINE "/CASE=" $R9
  StrCpy $DeleteAppDataCheckboxState 1
  StrCpy $UpdateMode 0
  StrCpy $PassiveMode 0
  SetSilent normal
  \${If} $R9 == "unchecked"
    StrCpy $DeleteAppDataCheckboxState 0
  \${ElseIf} $R9 == "update"
    StrCpy $UpdateMode 1
  \${ElseIf} $R9 == "passive"
    StrCpy $PassiveMode 1
  \${ElseIf} $R9 == "silent"
    SetSilent silent
  \${EndIf}
  !insertmacro WELFARE_DELETE_USER_DATA_IF_REQUESTED "$EXEDIR\\$R9"
  SetAutoClose true
SectionEnd
`, 'utf8');
        execFileSync(process.env.NSIS_COMPILER, ['/V2', script], {windowsHide: true, timeout: 30_000});
        const file = (name, contents = 'disposable test data') => {
            mkdirSync(path.dirname(name), {recursive: true});
            writeFileSync(name, contents);
        };
        const run = name => execFileSync(exe, [`/CASE=${name}`], {windowsHide: true, timeout: 30_000});

        for (const name of ['checked', 'unchecked', 'update', 'passive', 'silent']) {
            const profile = path.join(temporary, name, 'welfare-scraper');
            for (const target of ['.env', 'config.json', 'config.json.bak', 'config_corrupt_x.json', 'config_x.json.tmp',
                'welfare.db', 'welfare.db-wal', 'welfare.db-shm', 'attachment-text-cache.sqlite3', 'ai_quota_state.json', 'ai_quota_state.json.minute.json', 'ai_quota_state.json.lock', 'ai_quota_state.json.exhausted.0123456789abcdef0123456789abcdef.json',
                'backups/welfare_backup_20260908_test.db', 'logs/api_server.log', 'logs/api_server.log.1', 'temp/WelfareF5_report_test.html']) {
                file(path.join(profile, target));
            }
            for (const preserved of ['api-server.exe', 'welfare-tauri.exe', 'my-notes.txt', 'backups/my-manual.db', 'logs/my-notes.txt', 'temp/my-notes.txt', 'unknown-dir/file.txt']) {
                file(path.join(profile, preserved));
            }
            run(name);
            for (const target of ['.env', 'config.json', 'config.json.bak', 'config_corrupt_x.json', 'config_x.json.tmp',
                'welfare.db', 'welfare.db-wal', 'welfare.db-shm', 'attachment-text-cache.sqlite3', 'ai_quota_state.json', 'ai_quota_state.json.minute.json', 'ai_quota_state.json.lock', 'ai_quota_state.json.exhausted.0123456789abcdef0123456789abcdef.json',
                'backups/welfare_backup_20260908_test.db', 'logs/api_server.log', 'logs/api_server.log.1', 'temp/WelfareF5_report_test.html']) {
                assert.equal(existsSync(path.join(profile, target)), name !== 'checked', `${name}: ${target}`);
            }
            for (const preserved of ['api-server.exe', 'welfare-tauri.exe', 'my-notes.txt', 'backups/my-manual.db', 'logs/my-notes.txt', 'temp/my-notes.txt', 'unknown-dir/file.txt']) {
                assert.ok(existsSync(path.join(profile, preserved)), `${name}: preserve ${preserved}`);
            }
        }
        for (const kind of ['root', 'backups', 'logs', 'temp']) {
            const name = `junction-${kind}`;
            const profile = path.join(temporary, name, 'welfare-scraper');
            const outside = path.join(temporary, `outside-${kind}`);
            file(path.join(outside, 'config.json'));
            file(path.join(outside, 'welfare_backup_20260908_test.db'));
            file(path.join(outside, 'api_server.log'));
            file(path.join(outside, 'WelfareF5_report_test.html'));
            const link = kind === 'root' ? profile : path.join(profile, kind);
            mkdirSync(path.dirname(link), {recursive: true});
            symlinkSync(outside, link, 'junction');
            junctions.push(link);
            run(name);
            for (const protectedFile of ['config.json', 'welfare_backup_20260908_test.db', 'api_server.log', 'WelfareF5_report_test.html']) {
                assert.ok(existsSync(path.join(outside, protectedFile)), `${kind}: protect ${protectedFile}`);
            }
        }
    } finally {
        // Remove known links themselves before recursively removing our checked
        // mkdtemp directory, never a target or a computed shell command.
        for (const junction of junctions) unlinkSync(junction);
        rmSync(temporary, {recursive: true, force: true});
    }
});

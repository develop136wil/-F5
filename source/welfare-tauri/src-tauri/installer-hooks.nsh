; Never terminate processes by name during install/uninstall.
!macro WELFARE_REQUIRE_CLOSED
  Push $0
  Push $1
  ; Capture last-error inside this call: a second plugin call can overwrite it.
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\SearchMaster.WelfareF5.Desktop") p.r0 ?e'
  Pop $1
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_OK|MB_ICONEXCLAMATION "복지F5가 실행 중입니다. 앱에서 작업을 마무리하고 창을 닫은 뒤 다시 진행해주세요." /SD IDOK
    Abort
  ${Else}
    ${If} $1 != 2
      MessageBox MB_OK|MB_ICONEXCLAMATION "복지F5가 종료됐는지 확인하지 못했습니다. 기존 복지F5를 닫고 다시 진행해주세요." /SD IDOK
      Abort
    ${EndIf}
  ${EndIf}
  ; Legacy processes may not have the mutex. Check only this installation's
  ; exact desktop and backend paths; never search or terminate by process name.
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\${MAINBINARYNAME}.exe", i 0x80000000, i 0, p 0, i 3, i 0, p 0) p.r0 ?e'
  Pop $1
  ${If} $0 == -1
    ${If} $1 != 2
    ${AndIf} $1 != 3
      MessageBox MB_OK|MB_ICONEXCLAMATION "복지F5 실행 파일을 안전하게 확인하지 못했습니다. 기존 복지F5를 종료한 뒤 다시 진행해주세요." /SD IDOK
      Abort
    ${EndIf}
  ${Else}
    System::Call 'kernel32::CloseHandle(p r0)'
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\api-server.exe", i 0x80000000, i 0, p 0, i 3, i 0, p 0) p.r0 ?e'
  Pop $1
  ${If} $0 == -1
    ${If} $1 != 2
    ${AndIf} $1 != 3
      MessageBox MB_OK|MB_ICONEXCLAMATION "복지F5가 사용 중인 파일을 닫지 못했습니다. 기존 복지F5를 종료한 뒤 다시 진행해주세요." /SD IDOK
      Abort
    ${EndIf}
  ${Else}
    System::Call 'kernel32::CloseHandle(p r0)'
  ${EndIf}
  Pop $1
  Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro WELFARE_REQUIRE_CLOSED
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro WELFARE_REQUIRE_CLOSED
!macroend

; The backend profile is not the Tauri bundle-id directory. It can share its
; directory with an older installation, so NEVER recursively delete this root.
; BASE is a trusted shell folder (LOCALAPPDATA), never a config/registry value.
!macro WELFARE_DELETE_USER_DATA BASE
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  GetFullPathName $0 "${BASE}"
  GetFullPathName $1 "$0\welfare-scraper"
  ${GetParent} "$1" $2
  ${GetRoot} "$0" $3
  ${If} $0 != ""
  ${AndIf} $0 != $3
  ${AndIf} $2 == $0
    ; Refuse a junction/symlink profile instead of traversing another folder.
    System::Call 'kernel32::GetFileAttributesW(w r1) i.r3'
    IntOp $4 $3 & 0x410 ; FILE_ATTRIBUTE_DIRECTORY | REPARSE_POINT
    ${If} $3 != -1
    ${AndIf} $4 = 16
      Delete "$1\.env"
      Delete "$1\config.json"
      Delete "$1\config.json.bak"
      Delete "$1\config_*.json.tmp"
      Delete "$1\config_backup_*.tmp"
      Delete "$1\config_corrupt_*.json"
      Delete "$1\welfare.db"
      Delete "$1\welfare.db-wal"
      Delete "$1\welfare.db-shm"
      Delete "$1\welfare.db-journal"
      Delete "$1\attachment-text-cache.sqlite3"
      Delete "$1\attachment-text-cache.sqlite3-wal"
      Delete "$1\attachment-text-cache.sqlite3-shm"
      Delete "$1\attachment-text-cache.sqlite3-journal"
      Delete "$1\ai_quota_state.json"
      Delete "$1\ai_quota_state.json.minute.json"
      Delete "$1\ai_quota_state.json.lock"
      Delete "$1\ai_quota_state.json.exhausted.*.json"
      Delete "$1\ai_quota_*.tmp"
      Delete "$1\quota_state.json"
      Delete "$1\ai_job.lock"
      Delete "$1\scraper.lock"

      System::Call 'kernel32::GetFileAttributesW(w "$1\backups") i.r3'
      IntOp $4 $3 & 0x410
      ${If} $3 != -1
      ${AndIf} $4 = 16
        Delete "$1\backups\welfare_daily_backup_*.db"
        Delete "$1\backups\welfare_backup_*.db"
        Delete "$1\backups\welfare_pre_restore_*.db"
        RMDir "$1\backups" ; Only if empty; unknown files stay untouched.
      ${EndIf}
      System::Call 'kernel32::GetFileAttributesW(w "$1\logs") i.r3'
      IntOp $4 $3 & 0x410
      ${If} $3 != -1
      ${AndIf} $4 = 16
        Delete "$1\logs\*.log"
        Delete "$1\logs\*.log.*"
        RMDir "$1\logs"
      ${EndIf}
      System::Call 'kernel32::GetFileAttributesW(w "$1\temp") i.r3'
      IntOp $4 $3 & 0x410
      ${If} $3 != -1
      ${AndIf} $4 = 16
        Delete "$1\temp\WelfareF5_*"
        RMDir "$1\temp" ; Only app-owned direct files; never follow a junction.
      ${EndIf}
      RMDir "$1" ; Keeps unknown files and a shared application install intact.
      ${If} ${FileExists} "$1\*"
        DetailPrint "앱 폴더에 남은 파일이 있습니다. 별도로 넣은 파일이나 사용 중인 파일은 보존했습니다: $1"
      ${EndIf}
    ${Else}
      DetailPrint "데이터 폴더가 없거나 안전하게 확인되지 않아 보존했습니다: $1"
    ${EndIf}
  ${Else}
    DetailPrint "데이터 폴더 경로를 확인하지 못해 저장된 자료를 보존했습니다."
  ${EndIf}
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

; An unattended removal or an update must never imply data deletion.
!macro WELFARE_DELETE_USER_DATA_IF_REQUESTED BASE
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
  ${AndIf} $PassiveMode <> 1
  ${AndIfNot} ${Silent}
    !insertmacro WELFARE_DELETE_USER_DATA "${BASE}"
  ${EndIf}
!macroend

; Rename only shortcuts that point at this installation, never unrelated links.
!macro WELFARE_RENAME_SHORTCUT ROOT
  !insertmacro IsShortcutTarget "${ROOT}\welfare-scraper.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
  Pop $0
  ${If} $0 = 1
    ${IfNot} ${FileExists} "${ROOT}\복지F5.lnk"
      Rename "${ROOT}\welfare-scraper.lnk" "${ROOT}\복지F5.lnk"
    ${Else}
      !insertmacro IsShortcutTarget "${ROOT}\복지F5.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      Pop $0
      ${If} $0 = 1
        Delete "${ROOT}\welfare-scraper.lnk"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  Push $0
  !insertmacro WELFARE_RENAME_SHORTCUT "$SMPROGRAMS"
  !insertmacro WELFARE_RENAME_SHORTCUT "$DESKTOP"
  Pop $0
!macroend

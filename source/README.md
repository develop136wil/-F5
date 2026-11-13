# 복지F5 소스코드

[복지F5 소개](../README.md) · [AI 활용과 개선 방식](../docs/AI_IMPROVEMENT_EVIDENCE.md)

앱 실행 코드, 테스트, 빌드 설정, 아이콘·튜토리얼 및 로컬 OCR 모델을 포함합니다. **일반 이용자는 소스를 빌드하지 않고 메인 README의 설치본을 사용합니다.**

## 구성

| 경로 | 내용 |
| --- | --- |
| `welfare-tauri/src/` | React 화면·사용 흐름 |
| `welfare-tauri/src-tauri/` | Rust/Tauri 앱·설치 프로그램 설정 |
| `welfare-tauri/public/` | 앱에 포함되는 안내 이미지·매뉴얼 |
| `welfare_scraper/` | Python/FastAPI 서버와 `services/`의 수집·AI·검색·Notion 연결 |
| `welfare_scraper/assets/` | 주소 양식, 해시가 고정된 OCR 모델과 고지 |
| 각 프로젝트의 `tests/`, 백엔드 `scripts/` | 회귀 검사·격리된 빌드·배포 검증 도구 |
| `constraints-build.txt` | Windows 빌드·테스트에 사용한 Python 의존성 버전 |
| `build_app.bat` | 백엔드 빌드·소스 대조 → Tauri 설치본 → 배포 파일 검증 |

개인 `.env`·설정·API 키·토큰·사용자 DB·로그·백업, 개인 자료를 참조하는 일회성 검사와 생성된 빌드 파일은 제외했습니다. 예시 설정에는 실제 인증정보 대신 자리표시자를 사용합니다.

## 개발·빌드 준비

Windows x64 기준입니다. 빌드에 사용한 환경은 Python 3.14.2, Node.js 24.13.0, Rust 1.93.0입니다. Rust MSVC 도구 체인, Visual Studio C++ 빌드 도구·Windows SDK와 WebView2가 필요합니다. 의존성 다운로드에는 인터넷 연결이 필요합니다.

저장소의 `source/` 디렉터리에서 PowerShell로 실행합니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-build.txt -r welfare_scraper\requirements.txt -r requirements-dev.txt
npm --prefix welfare-tauri ci
.\build_app.bat
```

`.venv`는 반드시 `source/.venv`에 둡니다. 스크립트는 격리된 임시 프로필에서 백엔드를 빌드하고, 검증된 백엔드를 Tauri sidecar로 복사한 뒤 설치 EXE·ZIP을 `source/artifacts/release-candidates/`에 생성합니다. 검증 실패 파일은 배포하지 않습니다.

프런트엔드와 Rust 의존성은 `package-lock.json`·`Cargo.lock`을 사용합니다. Python은 위 명령처럼 `constraints-build.txt`를 함께 적용해 빌드·테스트에 사용한 버전으로 설치합니다. OCR 모델과 라이선스 파일은 동봉 manifest의 버전·크기·해시 검사에 통과해야 빌드됩니다. 의존성 버전을 바꾸면 관련 고지와 빌드 검사도 함께 확인합니다.

직접 빌드한 앱에서 템플릿 열기 기능을 사용하려면 `welfare-tauri/.env.example`을 참고해 **공개·복제 가능한 Notion 템플릿 URL**을 빌드 전에 개발 환경에 지정합니다. 예시 주소는 실제 템플릿이 아닙니다. `VITE_` 설정은 화면 번들에 포함되므로 비밀키·토큰을 넣으면 안 됩니다. 실제 서비스 연결은 앱에서 본인 계정으로 설정합니다.

## 외부 계정 없이 테스트하기

```powershell
npm --prefix welfare-tauri test
npm --prefix welfare-tauri run build
.\.venv\Scripts\python.exe -B welfare_scraper\scripts\run_isolated_tests.py tests.test_policy_evidence tests.test_policy_presend_budget
.\.venv\Scripts\python.exe -B welfare_scraper\scripts\run_isolated_tests.py tests.test_no_local_daily_cap_integration tests.test_site_test_jobs
```

백엔드 검사 도구는 임시 프로필·빈 인증정보로 실행합니다. **파일 격리는 네트워크 차단을 뜻하지 않으므로** 명시적으로 오프라인 테스트를 선택합니다. 전체 검사를 운영 프로필에서 무작정 실행하지 않습니다.

과거 실서비스 응답·현장 자료를 읽는 일부 회귀 테스트의 원본 `artifacts/`는 개인정보 보호를 위해 제외했습니다. 해당 테스트 정의는 남겨 두었지만 모든 테스트가 이 저장소만으로 실행되는 것은 아닙니다. 위 명령은 별도 실자료 없이 실행할 수 있는 범위입니다.

`welfare-tauri/tests/ui/`의 브라우저 사용 흐름 테스트에는 별도 Playwright JavaScript 의존성과 실행 중인 Vite 시험 서버가 필요합니다. `PLAYWRIGHT_MODULE`로 별도 설치 위치를 지정할 수 있습니다. 새 소식 템플릿 단위 검사는 개인 설정 없이 실행할 수 있도록 `.env.example`을 참조합니다.

## 권한·라이선스

복지F5 자체 소스코드는 [Apache License 2.0](../LICENSE), 자체 문서와 프롬프트는 [CC BY 4.0](../LICENSE-DOCS)을 적용합니다. 문서에는 README·docs·사용 설명서가 포함되며, 코드 안에 포함된 자체 자연어 프롬프트 문구에도 CC BY 4.0을 적용합니다. 프롬프트를 실행하는 프로그램 로직은 Apache 2.0을 따릅니다.

라이브러리·모델·폰트·외부 정책 원문·제3자 자료·기관 로고 등은 이 허락의 대상이 아니며, 각 권리자의 이용 조건을 따릅니다. [외부 구성요소 내역](../docs/OPERATIONS_AND_EVIDENCE.md#third-party)과 동봉된 기존 고지를 유지합니다. 재배포 시 해당 라이선스 전문, 저작자 표시 및 변경 사실 표시 등 적용 조건을 확인해 주세요.

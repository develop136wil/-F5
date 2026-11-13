# 복지F5 사용·관리 안내

[복지F5 소개](../README.md) · [시작 매뉴얼 PDF](manuals/복지F5_시작_매뉴얼.pdf)

계정 연결, 백업·업데이트, 자료 정리와 문제 해결 방법을 안내합니다.

<a id="operation"></a>

## 1. 백업·업데이트와 자료 관리

### 계정과 자료 관리

- 기관의 PC 보안정책과 외부 AI 이용 조건을 먼저 확인하고, 본인의 업무 권한에 맞는 Google·Notion 계정을 연결합니다.
- API 키·토큰은 PC의 설정 파일에 저장되며, 별도로 암호화되지 않습니다. 설정·DB·로그·백업을 그대로 공유하지 마세요. 같은 Windows 사용자 계정을 공유하면 앱 설정과 자료도 함께 사용하게 됩니다.
- AI 분석·의미 검색에 필요한 텍스트와 조건은 Google로 전송될 수 있습니다. 상담기록 등 민감자료는 넣지 마세요. 이미지·PDF를 외부 AI로 직접 보내는 기능에서는 전송 확인을 받습니다.
- Notion 등록·정책 반영은 사용자가 선택해 실행합니다. 예약 수집은 PC에 결과를 저장하고, 정책 자동 감시는 출처 변경을 알립니다.

### 업데이트·백업·복원

1. 진행 중인 작업을 마치고 DB를 백업한 뒤 앱을 완전히 종료합니다.
2. 안내된 다운로드 링크에서 설치본을 받아 업데이트합니다. 설치 후 기존 자료·연결·예약 설정을 확인하고 소수 자료로 동작을 확인합니다.
3. DB를 복원할 때는 현재 자료도 먼저 백업합니다. **DB 복원은 Notion에서 수정한 내용까지 되돌리지 않습니다.**

정책 변경은 반영 기록과 현재 Notion 값이 일치하는 등 조건을 충족할 때 취소할 수 있습니다. 이후 다른 수정이 있거나 기록이 없으면 취소하지 못할 수 있습니다. 완료 카드를 정리하면 해당 카드의 취소 버튼도 사용할 수 없으므로 결과를 먼저 확인하세요.

### 자료·기록 보관과 정리

| 설정 | 대상과 주의사항 |
| --- | --- |
| 새 소식 처리 기록 보관 | 자동 정리 안 함·7일·30일 선택. 오래된 미채택 기록을 정리하며 찾은 소식·저장 자료·Notion과 진행 중·재개 가능한 작업은 보호 |
| 수집 자료의 자동 보존기간 | 처리 기록과 별도 설정. 지정 기간보다 오래된 자료나 마감 후 30일이 지난 공고 등을 삭제할 수 있음. 0으로 설정하면 이 자동 삭제를 끔 |
| 만료 항목 아카이브 | 기본 검색에서 숨기는 기능으로, 삭제와 다름 |
| 수동 데이터·기록 정리 | 화면에 표시된 삭제 대상을 확인한 뒤 실행. 자동 정리 설정과 별도로 작동 |

7일·30일 보관을 선택하면 기존에 쌓인 미채택 기록도 정리 대상이 됩니다. 이후 앱 시작·수집 완료 시에도 자동 정리하며, 삭제한 기록은 앱에서 되돌릴 수 없습니다. 필요한 자료는 먼저 파일로 저장하거나 백업하세요.

### 예약 실행

새 소식 예약은 설정한 조건으로 수집·분석해 PC에 결과를 저장합니다. 앱이 꺼진 상태에서 실행하려면 Windows 작업 등록과 PC 전원·네트워크 연결이 필요합니다. 정책 자동 감시는 출처의 변경 여부를 알리는 기능이며, 기본값은 꺼짐입니다.

<a id="troubleshooting"></a>

## 2. 자주 겪는 문제와 해결 방법

| 상황 | 확인할 내용 |
| --- | --- |
| 다운로드·설치 경고가 나타남 | 배포 출처와 기관 PC 정책을 확인하세요. 확인할 수 없다면 실행하지 말고 문의하세요. 보안 프로그램을 일괄 해제하지 마세요. |
| 실행 구성요소 설치가 실패함 | WebView2 등 구성요소의 다운로드·설치가 기관 PC와 네트워크에서 허용되는지 확인하세요. |
| Notion 토큰을 저장했지만 접근할 수 없음 | 복제한 페이지에 연결의 접근 권한을 추가했는지, 올바른 페이지 주소인지 확인하세요. |
| 정책표가 비어 있어 점검할 것이 없음 | 상세 정책 URL로 초안을 만들어 검토·등록하거나, 원문에서 확인한 내용과 출처를 템플릿에 직접 입력하세요. |
| 원문 길이·민감정보·AI 전송 보호로 분석이 보류됨 | 해당 정책·공고의 상세 출처와 입력 내용을 확인하세요. 의미가 달라지도록 원문을 임의로 자르지 마세요. 보류된 자료는 ‘변경 없음’으로 판단하지 않습니다. |
| 새 소식이 0건임 | 게시판 목록 주소, 수집 조건·기간·제외 단어와 처리 기록을 확인하세요. 읽은 글이 모두 조건에 맞는 소식으로 채택되는 것은 아닙니다. |
| 새 소식에 정리된 본문 대신 원문 확인 안내가 나옴 | 항목별 정리 내용을 검증하기 어려운 자료는 본문 표시를 보류합니다. 원문에서 지원 내용·신청 조건과 첨부파일을 확인하세요. |
| 사이트 점검이 오래 걸림 | 화면을 다시 열어 진행 상태를 확인하세요. 앱을 종료했다면 점검을 다시 시작하세요. |
| 수집이 느리거나 일부 사이트에서 실패함 | 사이트 응답·첨부 읽기·AI 대기 상태를 확인하세요. 접근 제한이나 페이지 구조 때문에 수집하지 못하는 사이트도 있습니다. |
| HWP·PDF·이미지의 내용을 읽지 못함 | 파일 구조·손상·이미지 품질 또는 처리시간 제한이 원인일 수 있습니다. 원문을 직접 열어 누락된 조건을 확인하세요. OCR 결과도 원문 대조가 필요합니다. |
| AI 한도 안내가 나타남 | 본인 Google 계정의 사용 한도·요금 조건을 확인하세요. 분당 한도는 기다린 뒤, 일일 한도는 갱신 후 다시 시도하세요. |
| Notion 반영 결과가 불확실함 | 반복 등록하지 말고 현재 Notion 내용과 처리 상태부터 확인하세요. |
| Notion 없이 사용하고 싶음 | 새 소식 수집·HTML 파일 저장은 가능합니다. 정책 최신화·Notion 등록에는 연결이 필요합니다. |
| 저장한 파일의 이미지가 보이지 않음 | HTML의 포스터·원문 링크는 외부 주소일 수 있습니다. 인터넷 연결과 원본이 유지되는지 확인하세요. |

**사이트 점검은 게시판을 읽을 수 있는지 1페이지로 확인하는 기능**입니다. 설정을 바꾸거나 발견한 글을 새 소식·Notion에 저장하지 않습니다. 대표 홈페이지나 개별 안내 페이지에서는 게시글 목록을 찾지 못할 수 있습니다.

새 소식은 등록한 기관 게시판과 그 게시글의 상세 페이지·첨부파일을 읽습니다. 새 소식 수집 조건과 정책 최신화 조건은 각각 설정합니다.

주소의 Excel·CSV 일괄 등록과 저장 자료의 CSV·XLSX 내보내기를 지원합니다. 기존 Excel 정책표를 양방향으로 동기화하는 기능은 아직 제공하지 않습니다.

앱 자체의 일일 실행 횟수 제한은 없으며, AI 요청은 Google 사용 한도에 맞춰 대기합니다. Gemini API의 모델·사용 한도·요금은 프로젝트 등급과 모델에 따라 다릅니다. 사용 전 [Google AI Studio](https://aistudio.google.com/projects)에서 현재 조건을 확인하세요. [Google API 요금 안내](https://ai.google.dev/gemini-api/docs/billing)

<a id="release"></a>

## 3. 설치 파일 정보

| 구분 | 정보 |
| --- | --- |
| EXE | 복지F5 설치 파일 |
| ZIP | 복지F5 설치 EXE를 담은 압축 파일. 매뉴얼은 별도 제공 |
| 게시자 서명 | 없음 (`NotSigned`) |

EXE·ZIP은 [Google Drive 다운로드 폴더](https://drive.google.com/drive/folders/1yswvxtSYyUgepmn7wqWYQmeLWKEPmly7?usp=drive_link)에서 받을 수 있습니다. [시작 매뉴얼 PDF](manuals/복지F5_시작_매뉴얼.pdf)는 별도 파일입니다.

<a id="third-party"></a>

## 4. 외부 구성요소·자료

복지F5에서 사용하는 주요 외부 구성요소와 자료의 출처입니다.

| 자료·프로그램 | 목적 | 출처 | 라이선스·조건 |
| --- | --- | --- | --- |
| React / Tailwind CSS / Vite | 화면·빌드 | [React](https://github.com/facebook/react), [Tailwind](https://github.com/tailwindlabs/tailwindcss), [Vite](https://github.com/vitejs/vite) | MIT |
| Tauri | 데스크톱 앱 | [Tauri](https://github.com/tauri-apps/tauri) | MIT 또는 Apache-2.0 |
| Lucide | 아이콘 | [Lucide](https://github.com/lucide-icons/lucide) | lucide-react: ISC |
| FastAPI / Uvicorn | 로컬 서버 | [FastAPI](https://github.com/fastapi/fastapi), [Uvicorn](https://github.com/encode/uvicorn) | MIT / BSD-3-Clause |
| Playwright | 웹 수집 | [Playwright](https://github.com/microsoft/playwright-python) | Apache-2.0 |
| Beautiful Soup / openpyxl | HTML·Excel | [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/), [openpyxl](https://openpyxl.readthedocs.io/) | MIT |
| aiohttp / APScheduler | 통신·예약 | [aiohttp](https://github.com/aio-libs/aiohttp), [APScheduler](https://github.com/agronholm/apscheduler) | Apache-2.0 AND MIT / MIT |
| pandas / NumPy | 자료·벡터 처리 | [pandas](https://github.com/pandas-dev/pandas), [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause. NumPy는 포함 구성요소별 추가 고지 적용 |
| pypdf | PDF 텍스트 | [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause |
| pypdfium2 / PDFium | PDF 렌더링 | [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | 래퍼 Apache-2.0 / BSD-3-Clause, PDFium 및 포함 라이브러리별 고지 |
| RapidOCR / OCR 모델 | 로컬 문자 인식 | [RapidOCR](https://github.com/RapidAI/RapidOCR), 소스에 포함된 모델 manifest | Apache-2.0, 모델별 출처·고지 포함 |
| ONNX Runtime | OCR 추론 | [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT 및 제3자 고지 |
| Pillow / olefile | 이미지·구형 문서 | [Pillow](https://github.com/python-pillow/Pillow), [olefile](https://github.com/decalage2/olefile) | MIT-CMU / BSD 및 기반 PIL 고지 |
| opencv-python | 이미지 처리 | [OpenCV Python](https://github.com/opencv/opencv-python) | 래퍼 MIT, OpenCV 바이너리 Apache-2.0 및 제3자 고지 |
| Shapely / GEOS | OCR 기하 처리 | [Shapely](https://github.com/shapely/shapely), [GEOS](https://libgeos.org/) | BSD-3-Clause / LGPL-2.1 |
| pyclipper / Clipper | OCR 영역 처리 | [pyclipper](https://github.com/fonttools/pyclipper) | MIT / Boost-1.0 |
| OmegaConf / ANTLR runtime / PyYAML | OCR 설정·의존성 | [OmegaConf](https://github.com/omry/omegaconf), [ANTLR](https://www.antlr.org/), [PyYAML](https://pyyaml.org/) | BSD-3-Clause / BSD-3-Clause / MIT |
| Google Gen AI SDK | AI 연결 | [python-genai](https://github.com/googleapis/python-genai) | Apache-2.0, API는 별도 서비스 조건 |
| PyInstaller | 패키징 | [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPLv2-or-later 및 배포 관련 특별 예외 |
| 기관 홈페이지·복지로 자료 | 공개 공고·정책 | 결과별 원문 링크 | 출처별 이용 조건·저작권 적용 |
| Notion / Gemini / Discord·Google Chat | 저장·분석·알림 | 각 서비스 | 별도 이용 조건 |

시작 매뉴얼은 팀이 직접 제작했습니다. 앱의 설정 안내에 사용한 외부 이미지는 [이미지 출처](../source/welfare-tauri/public/guide-images/SOURCES.md)에 정리했습니다.

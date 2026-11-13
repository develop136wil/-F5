# 복지F5 설정 안내용 실제 화면 캡처 규격

설정 도우미에는 직접 확인한 실제 Google·Notion·복지F5 화면만 사용한다. 화면을 흉내 낸 그림이나
실제와 다른 버튼 위치는 넣지 않는다. 외부 서비스 화면이 바뀌면 캡처와 안내 문구를 함께 갱신한다.

## 반드시 지울 정보

- Google·Notion 계정 이메일, 프로필 사진, 워크스페이스 이름
- Gemini API 키, Notion 연결 토큰, 개인 프로젝트·페이지·데이터베이스 주소
- 브라우저 즐겨찾기, 다른 탭 제목, 알림, 최근 방문 기록

민감한 값을 흐리게만 처리하지 말고 단색 상자로 완전히 가린다. API 키나 토큰이 한 번이라도 캡처되었다면
그 키는 폐기하고 새로 발급한다.

## 캡처 방법

1. 안내 전용 시험 계정을 사용한다.
2. 브라우저 창을 1440×900으로 맞추고 필요한 메뉴만 보이게 자른다.
3. 눌러야 할 곳에는 빨간 테두리와 `①`, `②` 번호만 넣는다.
4. 설명 문장은 이미지 안에 넣지 않는다. 문구 수정과 번역은 앱의 텍스트에서 처리한다.
5. WebP 형식, 가로 1200px 이하, 한 장당 250KB 이하로 저장한다.
6. 100%와 125% Windows 배율에서 글자가 읽히는지 확인한다.

## 필요한 캡처 목록

| 파일 이름 | 실제 화면에서 보여 줄 곳 | 민감 정보 확인 |
| --- | --- | --- |
| `google-api-keys.webp` | Google AI Studio의 `Dashboard → API Keys` | 계정·프로젝트 이름 |
| `google-create-key.webp` | `Create API key` 버튼 | 프로젝트 이름 |
| `google-copy-key.webp` | 생성된 키의 `Copy` 버튼 | 키 값 전체 |
| `app-gemini-key.webp` | 복지F5 `설정 → 연결 관리 → Google Gemini API 키` | 입력한 키 |
| `notion-create-connection.webp` | Notion 개발자 화면의 `Internal connections → Create a new connection` | 계정·워크스페이스 |
| `notion-copy-token.webp` | `Configuration`의 권한과 `Installation access token` 복사 버튼 | 토큰 값 |
| `app-notion-token.webp` | 복지F5 `설정 → 연결 관리 → Notion 연결 토큰` | 입력한 토큰 |
| `notion-news-duplicate.webp` | 공개 새 소식 갤러리 오른쪽 위 복제 버튼 | 계정·워크스페이스 |
| `notion-news-connection.webp` | 복제한 갤러리의 `••• → Connections → Add connections` | 워크스페이스·카드 내용 |
| `notion-news-url.webp` | 새 소식 갤러리 전체 페이지와 브라우저 주소창 | 데이터베이스 주소 |
| `app-news-url.webp` | 복지F5 `새 소식 갤러리` 주소 입력·연결 확인 | 실제 데이터베이스 주소 |
| `notion-template-duplicate.webp` | 공개 정책표 오른쪽 위 `Duplicate(복제)` | 계정·워크스페이스 |
| `notion-add-connections.webp` | 복제한 정책표의 `••• → Connections → Add connections` | 워크스페이스·페이지 내용 |
| `notion-copy-policy-url.webp` | 정책표 전체 페이지와 브라우저 주소창 | 데이터베이스 주소 |
| `app-policy-url.webp` | 복지F5 `정책 최신화용 정책표` 주소 입력·연결 확인 | 실제 데이터베이스 주소 |

## 코드 연결 규칙

검수가 끝난 파일만 이 폴더에 추가하고 `SetupWalkthrough.jsx`의 해당 단계에
`image`와 구체적인 `imageAlt`를 지정한다. 파일이 준비되지 않은 단계는 텍스트 안내만 표시하며,
빈 이미지나 임시 그림을 노출하지 않는다.

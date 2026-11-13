export const GOOGLE_AI_STUDIO_API_KEY_URL = 'https://aistudio.google.com/api-keys';
export const GOOGLE_GEMINI_API_KEY_HELP_URL = 'https://ai.google.dev/gemini-api/docs/api-key';
export const NOTION_INTEGRATIONS_URL = 'https://app.notion.com/developers/connections';
export const NOTION_CONNECTION_HELP_URL = 'https://developers.notion.com/guides/get-started/internal-connections';

// 공개 템플릿을 만든 뒤 welfare-tauri/.env의 VITE_NOTION_TEMPLATE_URL에 주소를 넣으면
// 튜토리얼과 설정 화면 양쪽에 같은 "템플릿 복제" 버튼이 자동으로 나타난다.
export const NOTION_TEMPLATE_URL = (import.meta.env.VITE_NOTION_TEMPLATE_URL || 'https://db136kidwil.notion.site/40a8ade502108285a05a01d6f05c8940?v=f5f8ade502108334acf808cfd241ec9e').trim();

// 새소식 갤러리는 정책 보관함과 속성이 다르다. 검수·복제 허용을 마친 공개 갤러리
// 주소가 있을 때만 입력란 가까이에 복제 버튼을 노출한다.
export const NOTION_NEWS_TEMPLATE_URL = (import.meta.env.VITE_NOTION_NEWS_TEMPLATE_URL || '').trim();

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const upload = await readFile(new URL('../src/pages/NotionUpload.jsx', import.meta.url), 'utf8');
const api = await readFile(new URL('../src/api/welfare.js', import.meta.url), 'utf8');
const setup = await readFile(new URL('../src/components/onboarding/GuidedSetup.jsx', import.meta.url), 'utf8');
const detail = await readFile(new URL('../src/components/ItemDetailModal.jsx', import.meta.url), 'utf8');
const profileRun = await readFile(new URL('../src/components/NewsProfileRunPanel.jsx', import.meta.url), 'utf8');
const actions = await readFile(new URL('../src/utils/newsAction.js', import.meta.url), 'utf8');
// Public template configuration keeps this test independent of personal settings.
const appEnv = await readFile(new URL('../.env.example', import.meta.url), 'utf8');

test('새소식 화면에서 Notion과 HTML 출력을 분리하고 HTML은 Notion 없이 허용한다', () => {
    assert.match(upload, /소식 모음 파일 저장/);
    assert.match(upload, /Notion에 등록/);
    assert.match(upload, /소식 모음 파일 저장은 Notion 없이도 사용할 수 있습니다/);
    assert.match(upload, /AI 사용 0회/);
});

test('HTML 갤러리는 선택 ID를 blob으로 받아 날짜가 붙은 파일로 저장한다', () => {
    assert.match(api, /post\('\/news\/gallery-html', \{ ids \}/);
    assert.match(api, /responseType: 'blob'/);
    assert.match(api, /WelfareF5_News_\$\{todayStr\(\)\}\.html/);
});

test('초기 설정은 새소식 템플릿 자리와 정확한 네 속성을 안내한다', () => {
    assert.match(setup, /NOTION_NEWS_TEMPLATE_URL/);
    assert.match(setup, /<SetupStageInstructions kind="news" phase="register"/);
    assert.match(setup, /이름\(제목\).*기관\(텍스트\).*선택\(선택\).*기간\(날짜\)/s);
    assert.match(appEnv, /^VITE_NOTION_NEWS_TEMPLATE_URL=https:\/\/your-workspace\.notion\.site\/your-reviewed-news-gallery$/m);
});

test('첨부파일 미확인 상태는 목록과 상세창에서 원문 확인을 안내한다', () => {
    assert.match(upload, /attachment_review_required/);
    assert.match(upload, /원문 확인 필요/);
    assert.match(detail, /source_attachments\?\.status === 'review_required'/);
    assert.match(detail, /요약에 빠진 내용이 있을 수/);
});

test('AI 입력 생략도 목록·상세·내보내기 전 확인에 포함된다', () => {
    assert.match(actions, /item\.attachment_review_required \|\| item\.input_review_required/);
    assert.match(upload, /it\.input_note/);
    assert.match(detail, /\['partial', 'empty'\]\.includes\(ai\?\.source_input\?\.status\)/);
    assert.match(detail, /ai\.source_input\.note/);
});

test('미확인 첨부가 있는 새소식은 Notion 등록과 HTML 저장 전에 확인한다', () => {
    assert.match(upload, /runAction\('upload', selectedIds/);
    assert.match(upload, /runAction\('html', selectedIds/);
    assert.match(upload, /newsActionConfirmation\(kind, snapshot\)/);
    assert.doesNotMatch(upload, /window\.confirm/);
});

test('프로필 수집은 로컬 첨부 지원 범위와 본문 확인 실패를 숨기지 않는다', () => {
    assert.match(profileRun, /attachment_review/);
    assert.match(profileRun, /한글·PDF·이미지는 먼저 PC에서 읽으며/);
    assert.match(profileRun, /content_failed/);
});

test('첨부 재사용은 수집 생략이 아니라 변경 확인 후 이전 내용을 사용하는 것으로 안내한다', () => {
    assert.match(profileRun, /attachments_reused/);
    assert.match(profileRun, /변경이 없어, 이전에 읽은 내용을 다시 사용/);
});

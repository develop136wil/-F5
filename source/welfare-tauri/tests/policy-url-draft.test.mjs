import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const apiSource = readFileSync(new URL('../src/api/welfare.js', import.meta.url), 'utf8');
const pageSource = readFileSync(new URL('../src/pages/PolicyManage.jsx', import.meta.url), 'utf8');
const tutorialSource = readFileSync(new URL('../src/pages/Tutorial.jsx', import.meta.url), 'utf8');

test('URL 정책 초안은 미리보기와 실제 생성을 서로 다른 API로 호출함', () => {
    assert.match(apiSource, /\/policy\/draft-from-url\/preview/);
    assert.match(apiSource, /\/policy\/draft-from-url\/create/);
    assert.match(apiSource, /timeout: 300000/);
    assert.match(pageSource, /이 단계에서는 원문을 읽고 미리보기만 만듭니다\. Notion은 아직 바뀌지 않습니다/);
    assert.match(pageSource, /확인하고 Notion에 추가/);
    assert.match(pageSource, /aria-expanded=\{urlDraftOpen\}/);
});

test('URL 정책 초안 기능을 정책 최신화 사용 가이드에서도 찾을 수 있음', () => {
    assert.match(tutorialSource, /직접 찾은 정책으로 초안 만들기/);
    assert.match(tutorialSource, /확인·수정한 뒤 Notion에 추가/);
});

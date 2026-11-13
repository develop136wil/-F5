import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const editorUrl = new URL('../src/components/NewsProfileEditor.jsx', import.meta.url);
const apiUrl = new URL('../src/api/welfare.js', import.meta.url);

test('자동 연계에서는 별도 복지로 선택과 표본 조회를 표시하지 않는다', async () => {
    const [editor, api] = await Promise.all([readFile(editorUrl, 'utf8'), readFile(apiUrl, 'utf8')]);
    assert.doesNotMatch(editor, /복지로|loadBokjiroSample|어디에서 찾을까요/);
    assert.match(editor, /대상 범위 정하기/);
    assert.match(api, /post\('\/news\/bokjiro-sample'/);
});

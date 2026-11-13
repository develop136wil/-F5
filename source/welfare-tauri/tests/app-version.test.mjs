import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('화면과 설치 패키지가 같은 버전을 사용한다', async () => {
    const readJson = async path => JSON.parse(await readFile(new URL(path, import.meta.url), 'utf8'));
    const pkg = await readJson('../package.json');
    const lock = await readJson('../package-lock.json');
    const tauri = await readJson('../src-tauri/tauri.conf.json');
    const layout = await readFile(new URL('../src/components/layout/Layout.jsx', import.meta.url), 'utf8');
    assert.equal(pkg.version, tauri.version);
    assert.equal(pkg.version, lock.version);
    assert.equal(pkg.version, lock.packages[''].version);
    assert.match(layout, /import \{ version as appVersion \} from '\.\.\/\.\.\/\.\.\/package\.json'/);
    assert.match(layout, /v\{appVersion\}/);
    assert.doesNotMatch(layout, /v\d+\.\d+\.\d+/);
});

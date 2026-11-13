import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

test('새 소식 해제는 주소 등록 단계에만 표시하고 공통 연결은 변경하지 않는다',()=>{
 const source=readFileSync(new URL('../src/components/onboarding/GuidedSetup.jsx',import.meta.url),'utf8');
 const handler=source.split('const disconnectNewsGallery = async () => {')[1].split('const saveNewsGallery')[0];
 assert.match(handler,/window\.confirm/);
 assert.match(handler,/saveDraftConfig\(\{ NOTION_DATABASE_ID: '' \}\)/);
 assert.doesNotMatch(handler,/NOTION_TOKEN\s*:|NOTION_POLICY_DBS\s*:/);
 assert.match(handler,/settingsDrafts\.isWriting\(\)/);
 assert.match(handler,/notion_configured: false/);
 assert.match(handler,/isCurrentView\(\).*setEditing\('news'\)/);
 assert.match(source,/config\?\.notion_configured && <button[^>]*onClick=\{disconnectNewsGallery\}[^>]*>새 소식 보관함 연결 해제<\/button>\}\s*<SetupStageInstructions kind="news" phase="register"/);
});

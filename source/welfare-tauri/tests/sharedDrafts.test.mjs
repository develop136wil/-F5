import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createSettingsDraftStore} from '../src/utils/settingsDrafts.js';
import {profileDraftKey,profileDraftValue,commitProfileDraft} from '../src/utils/profileDraft.js';
const p = (id='one', revision=1) => ({profile_id:id,revision,name:'시험 기준',region:'전국',min_age_months:null,max_age_months:null,
    life_cycles:[],characteristics:[],keywords:['복지'],excluded_keywords:[],site_ids:[],sources:[],ai_prompt:'',semantic_text:'복지 소식',relevance_enabled:true});
const read = path => fs.readFileSync(new URL(path,import.meta.url),'utf8');

test('도우미와 기본 설정은 같은 필드의 초안을 사용한다', () => {
    const guide=read('../src/components/onboarding/GuidedSetup.jsx'), settings=read('../src/pages/Settings.jsx');
    for(const key of ['apiKey','notionToken','notionDbId','policyDbs']) {
        assert.ok(guide.includes(`useSettingsDraft('${key}'`));assert.ok(settings.includes(`useSettingsDraft('${key}'`));
    }
    assert.doesNotMatch(settings,/hydrate\(\{apiKey: config.GOOGLE_API_KEY/);
    assert.match(guide,/settingsDrafts.runWrite/);
    assert.match(guide,/<DefaultNewsConditionEditor/);assert.match(settings,/<DefaultNewsConditionEditor/);
    assert.match(read('../src/components/DefaultNewsConditionEditor.jsx'),/useSettingsDraft\('defaultNewsConditionDraft'/);
});
test('도우미 재조회가 기본 설정의 입력을 덮지 않고 저장 후 함께 정상값이 된다', () => {
    const s=createSettingsDraftStore();s.ensure('targetRegion','도봉');s.edit('targetRegion','노원');
    s.hydrate({targetRegion:'도봉'},s.capture());assert.equal(s.get('targetRegion'),'노원');
    s.commit('targetRegion','노원');assert.equal(s.dirty('targetRegion'),false);
});
test('프로필마다 편집 초안과 서버 버전을 별도로 보존한다', () => {
    const s=createSettingsDraftStore(), a=profileDraftKey('one'), b=profileDraftKey('two');
    s.ensure(a,null);s.ensure(b,null);s.hydrate({[a]:profileDraftValue(p()),[b]:profileDraftValue(p('two'))});
    s.edit(a,{...s.get(a),draft:{...s.get(a).draft,region:'노원'}});
    s.hydrate({[a]:profileDraftValue(p('one',2))});
    assert.equal(s.get(a).profile.revision,1);assert.equal(s.get(a).draft.region,'노원');assert.equal(s.get(b).draft.region,'전국');
});
test('프로필 저장 성공은 정규화된 새 버전을 정상값으로 기록한다', () => {
    const s=createSettingsDraftStore(), key=profileDraftKey('one');s.ensure(key,profileDraftValue(p()));
    s.edit(key,{...s.get(key),draft:{...s.get(key).draft,region:' 노원 '}});
    commitProfileDraft(s,key,s.get(key),{...p('one',2),region:'노원'});
    assert.equal(s.get(key).profile.revision,2);assert.equal(s.get(key).draft.region,'노원');assert.equal(s.dirty(key),false);
});
test('저장 중 재진입해 수정하면 새 입력과 새 서버 버전을 모두 보존한다', () => {
    const s=createSettingsDraftStore(), key=profileDraftKey('one');s.ensure(key,profileDraftValue(p()));
    const submitted=s.get(key);s.edit(key,{...submitted,draft:{...submitted.draft,region:'강북'}});
    commitProfileDraft(s,key,submitted,p('one',2));
    assert.equal(s.get(key).draft.region,'강북');assert.equal(s.get(key).profile.revision,2);assert.equal(s.dirty(key),true);
});
test('한 프로필의 변경 취소는 다른 프로필 초안을 버리지 않는다', () => {
    const s=createSettingsDraftStore();for(const id of ['one','two']) {const key=profileDraftKey(id);s.ensure(key,profileDraftValue(p(id)));s.edit(key,{...s.get(key),draft:{...s.get(key).draft,region:'노원'}});}
    s.discard({[profileDraftKey('one')]:s.capture()[profileDraftKey('one')]});
    assert.equal(s.dirty(profileDraftKey('one')),false);assert.equal(s.dirty(profileDraftKey('two')),true);
});
test('프로필 닫기는 입력 유지이고 저장은 서버 버전 검사를 유지한다', () => {
    const source=read('../src/components/NewsProfileEditor.jsx');
    assert.match(source,/편집 닫기 · 입력 유지/);
    assert.match(source,/saveNewsProfile\(profileId, profile.revision, value\)/);
    assert.match(source,/settingsDrafts.runWrite/);
    assert.match(source,/미저장 변경 버리기/);
});

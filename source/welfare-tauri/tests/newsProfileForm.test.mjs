import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import { editableSettings, parseTags, validateAges, ageHint, targetChanged, applyProfilePreset, toggleTag, normalizeCollectionSources, toggleCollectionSource } from '../src/utils/newsProfileForm.js';

test('대상 변경 안내는 표현 순서가 아닌 조건 변화에 반응', () => {
    const original = {region:'전국', min_age_months:null, max_age_months:120, life_cycles:['청소년','노년'], characteristics:[], interest_topics:[], keywords:['지원'], excluded_keywords:[]};
    const draft = {...original, min_age_months:'', max_age_months:'120', life_cycles:'노년, 청소년', keywords:'지원', name:'새 이름'};
    assert.equal(targetChanged(original, draft), false);
    for (const change of [{region:'서울'}, {max_age_months:'780'}, {characteristics:'한부모'}, {interest_topics:'주거'}, {keywords:'돌봄'}, {excluded_keywords:'모집'}]) {
        assert.equal(targetChanged(original, {...draft, ...change}), true);
    }
    assert.equal(targetChanged(original, {...draft, ai_prompt:'수정'}), false);
});

test('수정한 AI 기준 두 항목을 저장 설정에 그대로 전달하고 원본을 보존', () => {
    const original = {ai_prompt:'원래 요청', semantic_text:'원래 설명'};
    const draft = editableSettings(original);
    draft.ai_prompt = '';
    draft.semantic_text = '노년층 돌봄 소식';
    assert.equal(editableSettings(draft).ai_prompt, '');
    assert.equal(editableSettings(draft).semantic_text, '노년층 돌봄 소식');
    assert.equal(original.ai_prompt, '원래 요청');
    assert.equal(original.semantic_text, '원래 설명');
});

test('프로필 전용 메타데이터를 저장 요청에서 제외하고 목록을 복사', () => {
    const source = { profile_id: 'x', revision: 3, schema_version: 1, general_mode: 'legacy_infant_v1', keywords: ['원본'] };
    const value = editableSettings(source);
    assert.equal('profile_id' in value, false);
    assert.equal('general_mode' in value, false);
    value.keywords.push('추가');
    assert.deepEqual(source.keywords, ['원본']);
});
test('빈 연령은 제한 없음이며 잘못된 연령·역전 범위를 거부', () => {
    assert.deepEqual(validateAges('', ''), { start_age_months: null, end_age_months: null });
    assert.deepEqual(validateAges('780', ''), { start_age_months: 780, end_age_months: null });
    for (const value of ['-1', '1.5', '1e2', 'abc', '1801']) assert.throws(() => validateAges(value, ''));
    assert.throws(() => validateAges('120', '12'));
});
test('사용자 태그를 쉼표·줄바꿈으로 나누고 중복 제거', () => {
    assert.deepEqual(parseTags(' 청소년,노년\n청소년,, '), ['청소년', '노년']);
    assert.equal(toggleTag('노년, 청년', '노년'), '청년');
    assert.equal(toggleTag('노년', '장애인'), '노년, 장애인');
});
test('개월을 만 나이로 안내하고 빈칸은 제한 없음으로 표시', () => {
    assert.equal(ageHint('780'), '만 65세 (780개월)');
    assert.equal(ageHint('25'), '만 2세 1개월 (25개월)');
    assert.equal(ageHint(''), '나이 제한 없음');
});

test('수집 위치는 정해진 두 종류만 안정된 순서로 선택한다',()=>{
    assert.deepEqual(normalizeCollectionSources(['bokjiro','unknown','sites','sites']), ['sites','bokjiro']);
    assert.deepEqual(toggleCollectionSource(['sites'],'bokjiro',true), ['sites','bokjiro']);
    assert.deepEqual(toggleCollectionSource(['sites','bokjiro'],'sites',false), ['bokjiro']);
    assert.throws(()=>toggleCollectionSource([], 'unknown', true));
});

test('복지로 기준 빠른 선택은 수집처와 제외 단어를 보존하고 대상 기준만 채운다', () => {
    const draft = { name: '복사본', sources: ['sites'], site_ids: ['1'], excluded_keywords: '채용' };
    const result = applyProfilePreset(draft, {
        name: '한부모·조손 복지 소식', life_cycles: [], characteristics: ['한부모·조손'], interest_topics:['생활지원','주거'],
        keywords: ['한부모', '조손'], semantic_text: '한부모와 조손가족 지원',
    });
    assert.equal(result.name, '한부모·조손 복지 소식');
    assert.equal(result.characteristics, '한부모·조손');
    assert.equal(result.keywords, '한부모, 조손');
    assert.equal(result.interest_topics, '생활지원, 주거');
    assert.equal(result.excluded_keywords, '채용');
    assert.deepEqual(result.sources, ['sites']);
    assert.deepEqual(result.site_ids, ['1']);
});

test('대상 조건 편집은 만 나이 입력과 수집 사이트 선택을 제공한다',()=>{
    const source=readFileSync(new URL('../src/components/NewsProfileEditor.jsx',import.meta.url),'utf8');
    assert.match(source,/OptionalAgeMonthInput label="시작 나이"/);
    assert.match(source,/OptionalAgeMonthInput label="종료 나이"/);
    assert.match(source,/도봉구.*서울특별시.*전국/);
    assert.match(source,/시작 나이 <b>0세 2개월<\/b>, 종료 나이 <b>0세 6개월<\/b>/);
    assert.match(source,/수집할 기관 사이트/);
    assert.doesNotMatch(source,/어디에서 찾을까요/);
    assert.doesNotMatch(source,/복지로 공식 서비스/);
    assert.match(source,/toggleProfileSite/);
    assert.match(source,/어떤 도움이 필요한가요/);
    assert.match(source,/aria-pressed/);
    assert.doesNotMatch(source,/시작 연령 \(개월, 빈칸은 제한 없음\)/);
});

test('빠른 추천 세 개를 전체 분류로 오해하지 않게 안내한다',()=>{
    const source=readFileSync(new URL('../src/components/NewsProfileEditor.jsx',import.meta.url),'utf8');
    assert.match(source,/빠른 추천 3개/);
    assert.match(source,/자주 쓰는 조합 예시/);
    assert.match(source,/임신·출산, 아동, 청소년, 청년 등은 위 버튼에서 자유롭게 선택/);
});

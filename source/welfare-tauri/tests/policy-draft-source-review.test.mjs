import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
    draftSourceReviewItems, emptyDraftSourceChecks, updateDraftSourceCheck,
    draftSourceConfirmed, draftSourceReview, requireDraftSourceConfirmation, draftIncomeReviewRequired,
    draftSourceCoverageNotice,draftAgeRangeError,
} from '../src/utils/policyDraftSourceReview.js';

const allConfirmed = () => draftSourceReviewItems.reduce((state, {key}) =>
    updateDraftSourceCheck(state, key, true), emptyDraftSourceChecks());

test('부분 읽기 이유는 알려진 코드만 안내하고 승인과 분리한다',()=>{
    assert.match(draftSourceCoverageNotice({reasons:['html_byte_limit','fetch_failed','html_byte_limit','secret',{}]}),/원문 크기 제한 · 원문 접근 실패/);
    assert.equal(draftSourceCoverageNotice({reasons:['secret']}),'');
    assert.equal(draftSourceCoverageNotice({reasons:'html_byte_limit'}),'');
    assert.equal(draftSourceConfirmed(emptyDraftSourceChecks()),false);
});
test('월령 역전·잘못된 값은 저장 전 차단하고 빈 끝점은 허용한다',()=>{
    for(const [start,end] of [[0,0],[0,1800],[null,0],[1800,null],[null,null]])assert.equal(draftAgeRangeError({start_age_months:start,end_age_months:end}),'');
    assert.match(draftAgeRangeError({start_age_months:120,end_age_months:12}),/시작 월령/);
    for(const value of [-1,1801,1.5,NaN,Infinity,'12'])assert.match(draftAgeRangeError({start_age_months:value}),/정수/);
});

test('소득 기준 검토 안내는 알려진 코드에서만 표시되며 승인 여부를 바꾸지 않는다', () => {
    for (const value of [null, {}, {reasons: 'draft_income_review_required'}, {reasons: ['unknown']}]) {
        assert.equal(draftIncomeReviewRequired(value), false);
    }
    const review = {reasons: ['draft_income_review_required'], unverified_fields: {target_income: null}};
    assert.equal(draftIncomeReviewRequired(review), true);
    assert.deepEqual(draftSourceReview(review).fields, ['소득 조건']);
    assert.equal(draftSourceConfirmed(emptyDraftSourceChecks()), false);
    assert.match(draftSourceReviewItems[0].detail, /확인되지 않은 내용/);
});

test('원문 대조는 기본 미확인이며 단일 확인의 명시적 true가 필요하다', () => {
    let state = emptyDraftSourceChecks();
    assert.equal(draftSourceConfirmed(state), false);
    for (const {key} of draftSourceReviewItems) {
        const before = {...state};
        state = updateDraftSourceCheck(state, key, true);
        assert.equal(before[key], false, '기존 상태를 변조하지 않음');
    }
    assert.equal(draftSourceConfirmed(state), true);
    assert.deepEqual(draftSourceReviewItems.map(item => item.key), ['review']);
    for (const value of [undefined, null, false, 1, 'true', {}, []]) {
        assert.equal(draftSourceConfirmed(updateDraftSourceCheck(state, 'review', value)), false);
    }
    assert.equal(draftSourceConfirmed({...state, review: 'true'}), false);
    assert.equal(draftSourceConfirmed({cost: true, target: true, dates: true, contact: true}), false, '이전 확인 상태를 새 확인으로 재사용하지 않음');
    assert.equal(draftSourceConfirmed({privacy_reviewed: true, source_review_confirmed: true}), false);
});

test('필드·미리보기 변경의 초기화는 이전 확인 객체와 독립이고 해제도 즉시 반영된다', () => {
    const confirmed = allConfirmed();
    assert.equal(draftSourceConfirmed(confirmed), true);
    assert.equal(draftSourceConfirmed(updateDraftSourceCheck(confirmed, 'review', false)), false);
    const next = emptyDraftSourceChecks();
    assert.equal(draftSourceConfirmed(next), false);
    assert.equal(draftSourceConfirmed(confirmed), true, '새 미리보기 초기화가 이전 객체를 바꾸지 않음');
    assert.deepEqual(updateDraftSourceCheck(next, 'privacy_reviewed', true), next);
});

test('원문 대조 메타는 전사문과 알려진 항목만 표시하며 모델 진단과 내용을 섞지 않는다', () => {
    const source = '<script>실행하지 않는 전사문</script>\n무료라는 단어가 있는지 원본 대조';
    const input = {required: false, source_text: source, note: {raw: '비밀'}, reasons: [{raw: '임의 진단'}],
        unverified_fields: ['support_detail', 'contact', 'support_detail', '__proto__', {raw: '임의'}, 'unknown']};
    assert.deepEqual(draftSourceReview(input), {
        sourceText: source, sourceUnavailable: false, fields: ['지원 내용·비용', '연락처'],
    });
    assert.equal(input.source_text, source);
    assert.equal(draftSourceConfirmed(draftSourceReview(input)), false, 'required:false 메타가 사람의 확인을 대체하지 않음');
});

test('전사문 길이는 Python과 같은 Unicode 문자 수 경계이며 긴 문장을 조용히 자르지 않는다', () => {
    for (const text of ['가'.repeat(30000), '😀'.repeat(30000), '가'.repeat(29999) + '😀']) {
        const result = draftSourceReview({source_text: text});
        assert.equal(result.sourceText, text);
        assert.equal(result.sourceUnavailable, false);
    }
    for (const text of ['가'.repeat(30001), '😀'.repeat(30001), '가'.repeat(30000) + '😀']) {
        assert.deepEqual(draftSourceReview({source_text: text}), {sourceText: '', sourceUnavailable: true, fields: []});
    }
});

test('서버의 unverified_fields 사전은 값 대신 알려진 필드 이름만 대조 항목으로 표시한다', () => {
    const input = {source_text: '원본 대조용 문장', unverified_fields: {
        contact: '모델이 잘못 만든 전화번호', life_cycle: ['임의 대상'], target: {raw: '임의 대상'},
        deadline: '2099-01-01', related_urls: ['https://secret.invalid/never-open'], internal_prompt: '표시 금지',
    }};
    const result = draftSourceReview(input);
    assert.deepEqual(result.fields, ['연락처', '생애 주기', '대상', '접수 마감', '관련 주소']);
    assert.equal(JSON.stringify(result).includes('2099'), false);
    assert.equal(JSON.stringify(result).includes('secret.invalid'), false);
    assert.equal(input.unverified_fields.contact, '모델이 잘못 만든 전화번호');
});

test('없는·손상된 메타와 원문 없는 구형 응답은 확인 안내를 유지하고 렌더링 가능한 형태만 반환한다', () => {
    for (const value of [null, undefined, [], {}, {source_text: {}}, {source_text: 42}, {source_text: null}]) {
        assert.deepEqual(draftSourceReview(value), {sourceText: '', sourceUnavailable: true, fields: []});
    }
    assert.equal(draftSourceReview({source_text: ' \n\t '}).sourceUnavailable, true);
    assert.deepEqual(draftSourceReview({unverified_fields: 'contact'}).fields, []);
    assert.deepEqual(draftSourceReview({unverified_fields: Array(100).fill('unknown').concat('contact')}).fields, []);
});

const apiSource = readFileSync(new URL('../src/api/welfare.js', import.meta.url), 'utf8');
const method = apiSource.slice(apiSource.indexOf('    createPolicyDraftFromUrl: async'),
    apiSource.indexOf('    checkPolicyDraftStatus: async'));

test('실제 등록 API wrapper는 true 외 모든 확인 입력을 HTTP 전에 차단한다', async () => {
    let posts = 0;
    const client = {post: async () => {posts++; throw Error('전송되면 안 됨');}};
    const api = new Function('client', 'requireDraftSourceConfirmation', `return {${method}}`)(client, requireDraftSourceConfirmation);
    for (const choice of [undefined, false, null, 1, 'true', [], {}, {privacy_reviewed: true}]) {
        await assert.rejects(api.createPolicyDraftFromUrl('https://example.org/policy', 'test', {}, choice),
            error => error.code === 'POLICY_SOURCE_REVIEW_REQUIRED');
    }
    assert.equal(posts, 0);
});

test('명시적 확인은 별도 메타로 한 번만 전송되고 전사문·진단·동의를 필드에 추가하지 않는다', async () => {
    const calls = [], fields = {title: '모의 정책', support_detail: '확인한 내용'};
    const client = {post: async (...args) => {calls.push(args); return {data: {success: true}};}};
    const api = new Function('client', 'requireDraftSourceConfirmation', `return {${method}}`)(client, requireDraftSourceConfirmation);
    assert.deepEqual(await api.createPolicyDraftFromUrl('https://example.org/policy', 'test', fields, true), {success: true});
    assert.deepEqual(calls, [['/policy/draft-from-url/create', {
        url: 'https://example.org/policy', db_key: 'test', fields, source_review_confirmed: true,
    }, {timeout: 60000}]]);
    assert.deepEqual(fields, {title: '모의 정책', support_detail: '확인한 내용'});
    await assert.rejects(api.createPolicyDraftFromUrl('https://example.org/policy', 'test', fields));
    assert.equal(calls.length, 1, '직전 요청의 확인을 재사용하지 않음');
});

test('API 실패는 자동 재전송하거나 성공으로 바꾸지 않는다', async () => {
    let attempts = 0;
    const client = {post: async () => {attempts++; throw Error('모의 응답 끊김');}};
    const api = new Function('client', 'requireDraftSourceConfirmation', `return {${method}}`)(client, requireDraftSourceConfirmation);
    await assert.rejects(api.createPolicyDraftFromUrl('https://example.org/policy', 'test', {}, true), /모의 응답 끊김/);
    assert.equal(attempts, 1);
});

test('실제 화면은 원문 경고·plaintext와 단일 확인을 사용하고 등록 전에 현재 ref도 확인한다', () => {
    const page = readFileSync(new URL('../src/pages/PolicyManage.jsx', import.meta.url), 'utf8');
    assert.match(page, /aria-label="원문 대조 필요"/);
    assert.match(page, /AI가 읽은 문장 · 원본과 대조해주세요/);
    assert.match(page, /원본 이미지·문서를 대신하지 않습니다/);
    assert.match(page, /금액·대상·접수\/이용 기간·연락처를 확인해주세요/);
    assert.doesNotMatch(page, /네 항목을/);
    assert.match(page, /\{urlDraftSourceReview\.sourceText\}/);
    assert.match(page, /<textarea rows=\{rows\} aria-label=\{label\}/);
    const create = page.slice(page.indexOf('    const handleUrlDraftCreate ='), page.indexOf('    const handleUrlDraftStatus ='));
    assert.ok(create.indexOf('draftSourceConfirmed(urlDraftSourceChecksRef.current)') < create.indexOf("beginUrlDraftOperation('create')"));
    assert.match(create, /urlDraftPreview\.fields, true/);
    assert.match(page, /disabled=\{urlDraftLocked \|\| urlDraftNeedsCheck \|\| !urlDraftSourceConfirmed/);
    for (const [start, end] of [
        ['    const updateUrlDraftField =', '    const toggleUrlDraftOption ='],
        ['    const handleUrlDraftPreview =', '    const handleUrlDraftCreate ='],
    ]) assert.match(page.slice(page.indexOf(start), page.indexOf(end)), /resetUrlDraftSourceReview\(/);
    assert.match(page.slice(page.indexOf('    const changeUrlDraftInput ='), page.indexOf('    const beginUrlDraftOperation =')), /clearUrlDraftPreview\(/);
    assert.match(page, /\[urlDraftInput\.dbKey, resetUrlDraftSourceReview\]/);
});

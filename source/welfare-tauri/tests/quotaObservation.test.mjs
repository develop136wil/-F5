import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {QUOTA_MODELS,QUOTA_METRICS,quotaDraftFromPolicy,quotaObservationPayload,quotaPolicyStatusText} from '../src/utils/quotaObservation.js';
import {aiUsagePresentation} from '../src/utils/aiUsagePresentation.js';
const observed=Object.fromEntries(QUOTA_MODELS.map(model=>[model,{rpm:11,tpm:12345,rpd:321}]));

test('보호용 요청 집계를 실제 통신 횟수나 Google 사용량으로 안내하지 않는다',()=>{
    const settings=readFileSync(new URL('../src/pages/Settings.jsx',import.meta.url),'utf8');
    const usage=readFileSync(new URL('../src/components/AiUsageStatus.jsx',import.meta.url),'utf8');
    const observation=readFileSync(new URL('../src/components/AiQuotaObservation.jsx',import.meta.url),'utf8');
    const explanation='묶음 임베딩은 텍스트 한 건을 요청 한 회로 보수적으로 집계합니다. 실제 통신 횟수·Google 사용량과 다를 수 있습니다.';
    assert.ok(usage.includes(explanation));
    assert.doesNotMatch(settings,/<h[1-6][^>]*>AI 사용량 보호/);
    assert.match(settings,/href="https:\/\/aistudio.google.com\/usage"[^>]*>Gemini 사용량·한도 확인 ↗<\/a>/);
    assert.doesNotMatch(settings,/handleSaveAiLimit|<AiQuotaObservation|상한 저장/);
    assert.match(observation,/RPM 집계는 보호용 요청 단위.*TPM은 입력량 추정치/);
    assert.doesNotMatch(observation,/20회|0회 중지|7일이 지나면|앱 적용 상한/);
    assert.match(observation,/key!=='rpd'&&Number.isFinite/,'폐지한 앱 RPD 상한은 null을 0으로 표시하지 않음');
    const state=aiUsagePresentation({data:{local_budget_exhausted:true,recorded_requests_today:12,daily_request_limit:20}});
    assert.equal(state.count,12,'원장 값을 실제 통신 횟수로 환산하거나 바꾸지 않음');
    assert.equal(state.note,'');
    assert.equal(state.tone,'neutral');
});

test('한도 관측값이 없거나 만료되면 예시를 채우지 않고 모든 입력을 비운다',()=>{
    for(const policy of [undefined,null,{state:'unconfirmed'},{state:'reconfirmation_required',observed},{state:'invalid',observed}])
        assert.deepEqual(Object.values(quotaDraftFromPolicy(policy)).flatMap(Object.values),Array(6).fill(''));
    assert.deepEqual(quotaDraftFromPolicy({state:'current',observed}),Object.fromEntries(QUOTA_MODELS.map(model=>[model,{rpm:'11',tpm:'12345',rpd:'321'}])));
    assert.match(quotaPolicyStatusText({state:'current',observed_on:'2026-09-09'}),/2026-09-09.*7일 유효/);
    assert.match(quotaPolicyStatusText({state:'reconfirmation_required'}),/기간.*키.*다시/);
});

test('실제 확인과 두 모델의 여섯 정수만 API에 전달하고 빈값·범위초과를 거부한다',()=>{
    const draft=quotaDraftFromPolicy({state:'current',observed});
    assert.deepEqual(quotaObservationPayload(draft,true),{confirmed:true,models:observed});
    for(const confirmed of [undefined,false,1,'true'])assert.throws(()=>quotaObservationPayload(draft,confirmed),/확인란/);
    for(const {key,max} of QUOTA_METRICS){
        for(const value of ['',-1,1.5,'1e3','ten',true,Number.MAX_SAFE_INTEGER,max+1]){
            const changed=structuredClone(draft);changed[QUOTA_MODELS[0]][key]=value;
            assert.throws(()=>quotaObservationPayload(changed,true),/정수/);
        }
        for(const value of [0,max]){
            const changed=structuredClone(draft);changed[QUOTA_MODELS[0]][key]=String(value);
            assert.equal(quotaObservationPayload(changed,true).models[QUOTA_MODELS[0]][key],value);
        }
    }
    assert.deepEqual(draft,quotaDraftFromPolicy({state:'current',observed}),'검사 과정에서 초안을 바꾸지 않음');
});

test('관측 API는 confirmed true만 전달하며 하루 상한·키·날짜를 임의 변경하지 않는다',async()=>{
    const source=readFileSync(new URL('../src/api/welfare.js',import.meta.url),'utf8');
    const method=source.match(/^    saveQuotaObservation:.*$/m)[0],calls=[];
    const api=new Function('client',`return {${method}}`)({post:async(url,body)=>{calls.push({url,body});return {data:{status:'success'}};}});
    for(const confirmed of [false,'true',1,true]){
        await api.saveQuotaObservation({confirmed,models:observed});
        assert.deepEqual(calls.at(-1),{url:'/ai/quota-observation',body:{confirmed:confirmed===true,models:observed}});
    }
    const settings=readFileSync(new URL('../src/pages/Settings.jsx',import.meta.url),'utf8');
    assert.doesNotMatch(settings,/AiQuotaObservation|AI_DAILY_REQUEST_LIMIT: saved/,'일반 설정에서는 한도 편집을 제공하지 않음');
});

test('간결한 Gemini 안내와 펼쳐보는 처리 설명을 함께 제공한다',()=>{
    const settings=readFileSync(new URL('../src/pages/Settings.jsx',import.meta.url),'utf8');
    const collection=readFileSync(new URL('../src/components/NewsProfileRunPanel.jsx',import.meta.url),'utf8');
    assert.match(collection,/AI 분석은 Google의 사용 한도를 따릅니다/);
    assert.doesNotMatch(collection,/AI 분석에는 기존 사용량 제한|하루 AI 요청 상한|20회/);
    assert.match(settings,/AI 분석은 Google Gemini를 활용합니다/);
    assert.match(settings,/중복 요청과 과도한 재시도를 방지하고 요청 간격을 조절합니다/);
    assert.doesNotMatch(settings,/한도는 앱에서 관리|예약 실행에도 일일 AI 한도/);
    assert.match(settings,/<details[^>]*>[\s\S]*?AI 사용량 보호[\s\S]*?Google Gemini API에서 처리됩니다[\s\S]*?https:\/\/ai.google.dev\/gemini-api\/terms[\s\S]*?<\/details>/);
    assert.doesNotMatch(settings,/AI에는 개인정보 없는 업무 자료만|개인 사례나 비공개 문서는 입력하지/);
});

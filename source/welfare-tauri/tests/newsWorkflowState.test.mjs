import test from 'node:test';
import assert from 'node:assert/strict';
import {blocksNewWorkflow, canResumeWorkflow, canStartAdditionalCollection, analysisReadinessText, workflowStatusText, workflowRevisionText, workflowNotice, sourceProgressText, foregroundWorkflows} from '../src/utils/newsWorkflowState.js';

test('원문 형식 보류는 API 한도나 입력 분량 문제와 구분한다', () => {
    const notice = workflowNotice({state:'failed', error_code:'ai_source_review_required'});
    assert.match(notice, /생략 표시·본문 형식/);
    assert.match(notice, /원문은 보존/);
    assert.doesNotMatch(notice, /크기|한도|분량|잠시 후/);
});

test('이전 복지로 범위 기록은 보존하되 새 기관 수집을 막거나 재개하지 않는다',()=>{
    const old={workflow_id:'old',revision:2,state:'waiting',can_resume:false,resume_block_reason:'news_source_scope_changed'};
    assert.equal(blocksNewWorkflow(old,2),false);
    assert.equal(canResumeWorkflow(old),false);
    assert.match(workflowNotice(old),/자료는 보존.*등록한 기관 사이트/);
    assert.match(workflowRevisionText(old,3),/열람만 가능합니다/);
    assert.doesNotMatch(workflowRevisionText(old,3),/이어서 진행/);
    assert.deepEqual(foregroundWorkflows([old],2),[]);
    assert.equal(blocksNewWorkflow({...old,active:true},2),true,'실행 중인 작업은 안전하게 보호');
});

test('기본 화면에는 현재 작업 하나만 보이고 과거 실패·대기는 이력에서 보존한다',()=>{
    const current={workflow_id:'new',revision:3,state:'complete'};
    const older=[{workflow_id:'old1',revision:2,state:'failed'},{workflow_id:'old2',revision:2,state:'waiting'}];
    const items=[current,...older];
    assert.deepEqual(foregroundWorkflows(items,3),[current]);
    assert.equal(items.length,3);
    const pending={workflow_id:'pending',revision:3,state:'waiting'};
    assert.deepEqual(foregroundWorkflows([current,pending,...older],3),[pending]);
    const active={workflow_id:'active',revision:2,state:'running'};
    assert.deepEqual(foregroundWorkflows([current,active],3),[active]);
});

test('개별 사이트 실패는 전체 중단 대신 확인할 자료가 있는 완료로 알린다',()=>{
    const item={state:'complete',summary:{collection:{site_review_required:1,sources:{sites:{state:'finished',saved_total:12,site_review_required:1}}}}};
    assert.equal(workflowStatusText(item),'수집 완료 · 확인할 자료 있음');
    assert.match(sourceProgressText(item),/수집 완료 · 일부 사이트 확인 필요.*누적 저장 후보 12건.*확인 필요 1개 사이트/);
    assert.equal(canResumeWorkflow(item),false);
    const save={state:'waiting',can_resume:true,error_code:'site_save_failed'};
    assert.match(workflowNotice(save),/저장하지 못해.*저장 공간/);
    assert.doesNotMatch(workflowNotice(save),/잠시 후/);
});

test('별도 새 수집은 서버 승인과 수집 완료 분석 대기를 모두 요구한다',()=>{
    const waiting={revision:1,state:'waiting',stage:'analyzing',summary:{collection:{state:'finished'}}};
    const options={can_start_new_collection:true,selection:{revision:1}};
    assert.equal(canStartAdditionalCollection([waiting],options),true);
    for(const approved of [false,undefined,'true',1]) assert.equal(canStartAdditionalCollection([waiting],{...options,can_start_new_collection:approved}),false);
    for(const unsafe of [{...waiting,active:true},{...waiting,state:'ready'},{...waiting,state:'running'},{...waiting,stage:'collecting'},{...waiting,summary:{collection:{state:'waiting'}}}]) assert.equal(canStartAdditionalCollection([waiting,unsafe],options),false);
    assert.equal(canStartAdditionalCollection([],options),false);
    assert.equal(blocksNewWorkflow(waiting,1),true,'기존 기본/자동 시작의 중복 방지는 변경하지 않음');
});

test('AI 준비 안내와 수집 완료 분석 대기 상태를 실행 가능 여부와 분리한다',()=>{
    for(const ai of [{isLoading:true},{isError:true},{data:{busy:true}}]) {
        assert.match(analysisReadinessText({ai_required:true,analysis_ready:true},ai),/원문 수집/);
        assert.equal(analysisReadinessText({ai_required:false},ai),'');
    }
    assert.equal(analysisReadinessText({ai_required:true,analysis_ready:false,analysis_reason:'연결을 확인해주세요.'}),'연결을 확인해주세요.');
    assert.equal(analysisReadinessText({ai_required:true,analysis_ready:true},{data:{busy:false}}),'');
    assert.equal(workflowStatusText({state:'waiting',stage:'analyzing',summary:{collection:{state:'finished'}}}),'원문 수집 완료 · AI 분석 대기');
    assert.equal(workflowStatusText({state:'waiting',stage:'collecting',summary:{collection:{state:'waiting'}}}),'잠시 멈춤');
    for(const reason of ['analysis_settings_required','analysis_settings_changed','ai_usage_state_unavailable','ai_input_size_review_required']) assert.match(workflowNotice({state:'waiting',error_code:reason}),/원문/);
});

test('이전 조건 대기는 새 조건 실행을 막지 않고 실행 중 작업은 모든 버전에서 보호', () => {
    assert.equal(blocksNewWorkflow({state:'waiting',revision:1},2),false);
    assert.equal(blocksNewWorkflow({state:'waiting',revision:2},2),true);
    for (const state of ['ready','running']) assert.equal(blocksNewWorkflow({state,revision:1},2),true);
    assert.equal(blocksNewWorkflow({state:'waiting',revision:1,active:true},2),true);
    for (const state of ['complete','cancelled','failed']) assert.equal(blocksNewWorkflow({state,revision:2},2),false);
});

test('조건 버전이 없는 이전 기록은 중복 실행을 허용하지 않는다', () => {
    for (const revision of [undefined,null,0,-1,'1']) {
        assert.equal(blocksNewWorkflow({state:'waiting',revision},2),true);
        assert.equal(blocksNewWorkflow({state:'waiting',revision:1},revision),true);
    }
});

test('복지로 페이지 진행은 원본 총수·확인 누계·조건 일치 등록을 구분하고 부분 실패를 재개 안내한다',()=>{
    const source={state:'more_pages',total_reported:120,scanned:30,registered:12,pages_processed:3};
    const item={state:'running',summary:{collection:{sources:{bokjiro:source}}}};
    assert.equal(sourceProgressText(item),'복지로: 추가 페이지 확인 중 · 원본 목록 120건 · 확인한 원본 30건 · 조건에 맞아 저장 12건');
    assert.equal(workflowNotice(item),'');
    const waiting={...item,state:'waiting',can_resume:true,summary:{collection:{sources:{bokjiro:{...source,state:'waiting',stop_reason:'bokjiro_page_failed'}}}}};
    assert.match(sourceProgressText(waiting),/일부 페이지 확인 실패/);assert.match(workflowNotice(waiting),/확인하지 못한 페이지부터 다시 확인/);assert.equal(canResumeWorkflow(waiting),true);
    assert.equal(sourceProgressText({summary:{collection:{sources:{bokjiro:{state:'more_pages',total_reported:null,registered:0},sites:{state:'finished'},unknown:{state:'finished'}}}}}),'복지로: 추가 페이지 확인 중 · 조건에 맞아 저장 0건 · 기관 사이트: 확인 완료');
});

test('기관 일부 실패는 이번 호출 등록 수가 아닌 누적 후보를 안내하고 복구를 허용한다',()=>{
    const item={state:'waiting',can_resume:true,summary:{collection:{sources:{sites:{state:'waiting',stop_reason:'site_collection_failed',registered:15,saved_total:135}}}}};
    assert.equal(sourceProgressText(item),'기관 사이트: 일부 자료 확인 실패 · 누적 저장 후보 135건');
    assert.match(workflowNotice(item),/저장한 후보는 보존됩니다/);assert.equal(canResumeWorkflow(item),true);
    assert.equal(sourceProgressText({summary:{collection:{sources:{sites:{state:'finished',registered:15}}}}}),'기관 사이트: 확인 완료','이전 응답의 이번 등록 수를 누계로 오인하지 않음');
});

test('원문을 확보하지 못한 0건은 실제 빈 목록과 구분하며 완료 작업을 무한 재개하지 않는다',()=>{
    const item={state:'complete',summary:{collection:{skipped_candidates:2,sources:{sites:{state:'finished',saved_total:0,skipped_candidates:2}}}}};
    assert.match(sourceProgressText(item),/확인 완료 · 수집하지 못한 자료 있음/);
    assert.match(sourceProgressText(item),/누적 저장 후보 0건.*원문 링크·제목 확인 필요 2건/);
    assert.equal(canResumeWorkflow(item),false);
    const empty={state:'complete',summary:{collection:{sources:{sites:{state:'finished',saved_total:0,skipped_candidates:0}}}}};
    assert.equal(sourceProgressText(empty),'기관 사이트: 확인 완료 · 누적 저장 후보 0건');
    assert.doesNotMatch(sourceProgressText(empty),/확인 필요|수집하지 못한/);
});

test('대기와 서버가 복구 가능하다고 확인한 실패만 재개할 수 있다', () => {
    assert.equal(canResumeWorkflow({state:'waiting'}),true);
    assert.equal(canResumeWorkflow({state:'waiting',can_resume:false}),false);
    assert.equal(canResumeWorkflow({state:'failed',can_resume:true}),true);
    for(const state of ['ready','running','complete','cancelled']) assert.equal(canResumeWorkflow({state,can_resume:true}),false);
    assert.equal(canResumeWorkflow({state:'failed'}),false);
    assert.equal(canResumeWorkflow({state:'failed',can_resume:'true'}),false);
    assert.equal(canResumeWorkflow({state:'waiting',active:true}),false);
});

test('미확인 화면 이미지는 0건과 구분하고 자동 재개 대상으로 만들지 않는다',()=>{
    const item={state:'complete',summary:{collection:{sources:{sites:{state:'finished',saved_total:0,vision_review_sites:2}}}}};
    assert.match(sourceProgressText(item),/확인 완료 · 화면 이미지 확인 필요/);
    assert.match(sourceProgressText(item),/누적 저장 후보 0건.*화면 이미지 확인 필요 2개 사이트/);
    assert.equal(canResumeWorkflow(item),false);
    for(const value of [0,undefined,'2',-1]){
        item.summary.collection.sources.sites.vision_review_sites=value;
        assert.doesNotMatch(sourceProgressText(item),/화면 이미지 확인 필요/);
    }
});

test('수집에 실제 사용한 조건 버전과 재개 의미를 구분한다', () => {
    assert.equal(workflowRevisionText({revision:2},2),'현재 조건 · 버전 2');
    assert.match(workflowRevisionText({revision:1},2),/이전 조건.*이어서 진행해도 이전 조건/);
    assert.equal(workflowRevisionText({revision:1},null),'수집 조건 버전 1');
    assert.match(workflowRevisionText({},2),/사용한 조건/);
});

test('한도·연결·재시도·중단 사유를 안전한 안내로 제공한다', () => {
    for(const reason of ['daily_quota_exhausted','local_budget_exhausted','ai_input_review_required','authentication_required','retry_limit_reached','interrupted','shutdown','busy','batch_limit','analysis_failed']) {
        assert.ok(workflowNotice({state:'waiting',error_code:reason}).length>10,reason);
    }
    assert.match(workflowNotice({state:'waiting',summary:{analysis:{stop_reason:'batch_limit'}}}),/자동으로 계속 처리/);
    assert.match(workflowNotice({state:'failed',error_code:'analysis_failed',can_resume:true}),/이어서 진행/);
    assert.match(workflowNotice({state:'waiting',can_resume:false,error_code:'interrupted'}),/이어갈 수 없습니다.*종료하고 다시 수집/);
    assert.doesNotMatch(workflowNotice({state:'failed',error_code:'PRIVATE_EXCEPTION_TOKEN'}),/PRIVATE_EXCEPTION_TOKEN/);
    assert.equal(workflowNotice({state:'complete',error_code:'daily_quota_exhausted'}),'');
    assert.match(workflowNotice({state:'waiting',error_code:'local_budget_exhausted',can_resume:true}),/보관된 작업.*이어서 진행.*남은 자료/);
    assert.doesNotMatch(workflowNotice({state:'waiting',error_code:'local_budget_exhausted',can_resume:true}),/한도|상한|집계일|20회/);
    assert.match(workflowNotice({state:'failed',error_code:'ai_input_review_required',can_resume:false}),/AI 전송을 보류.*새 작업/);
});

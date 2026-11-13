const validRevision = value => Number.isInteger(value) && value > 0;

export function blocksNewWorkflow(item, currentRevision) {
    if (item.active || item.state === 'ready' || item.state === 'running') return true;
    if (item.resume_block_reason === 'news_source_scope_changed') return false;
    if (item.state !== 'waiting') return false;
    // Old records without a revision cannot safely be treated as a different condition.
    return !validRevision(item.revision) || !validRevision(currentRevision) || item.revision === currentRevision;
}

export function canResumeWorkflow(item) {
    if (item.resume_block_reason === 'news_source_scope_changed') return false;
    return !item.active && ((item.state === 'waiting' && item.can_resume !== false) || (item.state === 'failed' && item.can_resume === true));
}

// Keep old failed/paused runs available without filling the main news screen.
export function foregroundWorkflows(items, currentRevision) {
    const active = items.filter(item => item.active || ['ready','running'].includes(item.state));
    if (active.length) return active;
    const pending = items.find(item => blocksNewWorkflow(item, currentRevision));
    if (pending) return [pending];
    const latest = items.find(item => item.resume_block_reason !== 'news_source_scope_changed'
        && item.revision === currentRevision);
    return latest ? [latest] : [];
}

export function collectionNeedsReview(item) {
    const collection=item.summary?.collection || {};
    const sites=collection.sources?.sites || {};
    return ['site_review_required','vision_review_sites','skipped_candidates','content_failed','attachment_review']
        .some(key => Number(collection[key] || sites[key]) > 0);
}

// A separate collection is opt-in, and never replaces an unfinished collection.
export function canStartAdditionalCollection(items, options) {
    if (options?.can_start_new_collection !== true) return false;
    const blocking = items.filter(item => blocksNewWorkflow(item, options?.selection?.revision));
    return blocking.length > 0 && blocking.every(item => !item.active && item.state === 'waiting'
        && item.stage === 'analyzing' && item.summary?.collection?.state === 'finished');
}

export function analysisReadinessText(options, ai = {}) {
    if (!options?.ai_required) return '';
    if (options.analysis_ready === false) return options.analysis_reason || 'AI 분석 준비를 확인해주세요. 수집한 원문은 보관됩니다.';
    if (ai.isError) return 'AI 상태를 확인하지 못했습니다. 원문 수집은 가능하며, 분석 전에 상태를 다시 확인합니다.';
    if (ai.isLoading) return 'AI 상태 확인 중입니다. 원문 수집은 먼저 시작할 수 있습니다.';
    if (ai.data?.busy) return '다른 AI 작업이 진행 중입니다. 원문 수집은 가능하며, 분석은 AI가 사용 가능할 때 진행합니다.';
    return '';
}

export function workflowStatusText(item) {
    if (item.state === 'complete' && Number(item.summary?.analysis?.review_required_count) > 0) return '수집 완료 · 일부 분석 보류';
    if (item.state === 'complete' && collectionNeedsReview(item)) return '수집 완료 · 확인할 자료 있음';
    if (item.state === 'waiting' && item.stage === 'analyzing' && item.summary?.collection?.state === 'finished') return '원문 수집 완료 · AI 분석 대기';
    const states = {ready:'시작 준비',running:'진행 중',waiting:'잠시 멈춤',complete:'완료',failed:'완료하지 못함',cancelled:'사용자가 중지함'};
    const stages = {prepared:'준비됨',collecting:'선택한 곳에서 정보 확인 중',analyzing:'수집한 정보 정리 중',finished:'모든 단계 마침',failed:'작업 확인 필요',cancelled:'중지됨'};
    return item.state === 'running' ? `${states[item.state]} · ${stages[item.stage] || '단계 확인 필요'}`
        : (states[item.state] || stages[item.stage] || '상태 확인 필요');
}

export function workflowRevisionText(item, currentRevision) {
    if (item.resume_block_reason === 'news_source_scope_changed') return validRevision(item.revision)
        ? `이전 조건 · 버전 ${item.revision}. 이 기록은 열람만 가능합니다.`
        : '이전 수집 범위의 기록입니다. 이 기록은 열람만 가능합니다.';
    if (!validRevision(item.revision)) return '사용한 조건은 이 수집의 결과에서 확인할 수 있습니다.';
    if (!validRevision(currentRevision)) return `수집 조건 버전 ${item.revision}`;
    if (item.revision === currentRevision) return `현재 조건 · 버전 ${item.revision}`;
    return `이전 조건 · 버전 ${item.revision} (현재 ${currentRevision}). 이어서 진행해도 이전 조건을 사용합니다.`;
}

export function workflowNotice(item) {
    if (item.resume_block_reason === 'news_source_scope_changed') return '이전 수집 범위의 기록입니다. 자료는 보존되며, 새 소식 찾기에서 등록한 기관 사이트로 새로 수집해주세요.';
    const held = item.summary?.analysis?.review_required_count;
    if (item.state === 'complete' && Number.isInteger(held) && held > 0) return `${held.toLocaleString('ko-KR')}건은 별도 확인이 필요해 분석을 보류했고, 나머지 자료는 처리를 마쳤습니다. 보류한 원문과 처리 결과는 이 수집의 결과에서 확인할 수 있습니다.`;
    if (!['waiting', 'failed'].includes(item.state)) return '';
    if (item.state === 'waiting' && item.can_resume === false) return '일부 자료의 분석 오류로 이어갈 수 없습니다. 결과를 확인한 뒤 이 작업을 종료하고 다시 수집해주세요.';
    const reason = item.error_code || item.summary?.analysis?.stop_reason || item.summary?.collection?.sources?.bokjiro?.stop_reason || item.summary?.collection?.sources?.sites?.stop_reason;
    const notices = {
        site_collection_failed: '수집을 시작하거나 계속할 수 없습니다. 저장한 후보는 보존됩니다. 수집 사이트 설정과 연결 상태를 확인한 뒤 이어서 진행해주세요.',
        site_save_failed: '수집한 자료를 저장하지 못해 멈췄습니다. 이미 저장한 후보는 보존됩니다. 저장 공간과 파일 접근 상태를 확인한 뒤 이어서 진행해주세요.',
        bokjiro_page_failed: '복지로의 일부 페이지를 확인하지 못해 멈췄습니다. 확인한 자료는 보존됩니다. 잠시 후 이어서 진행하면 확인하지 못한 페이지부터 다시 확인합니다.',
        daily_quota_exhausted: 'AI 사용 한도에 도달해 멈췄습니다. 한도가 다시 사용 가능해진 뒤 이어서 진행할 수 있습니다.',
        local_budget_exhausted: '보관된 작업입니다. 이어서 진행하면 남은 자료를 처리합니다.',
        ai_input_review_required: '개인정보나 비밀정보가 포함되었을 가능성이 있어 AI 전송을 보류했습니다. 원문과 추가 요청을 확인한 뒤 새 작업을 시작해주세요.',
        authentication_required: 'AI 연결을 확인해야 합니다. 설정에서 Google API 키를 확인한 뒤 이어서 진행해주세요.',
        analysis_settings_required: '원문은 보관했습니다. 설정에서 AI 분석 모델과 기준을 준비한 뒤 이어서 진행해주세요.',
        analysis_settings_changed: '이 작업의 AI 분석 기준과 현재 설정이 다릅니다. 원문은 보존되며, 사용한 기준을 확인한 뒤 이어서 진행해주세요.',
        ai_usage_state_unavailable: 'AI 사용량 기록을 확인하지 못해 분석을 보류했습니다. 원문은 보존됩니다. 잠시 후 다시 확인해주세요.',
        ai_input_size_review_required: '입력 크기 확인이 필요해 AI 분석을 보류했습니다. 원문은 보존됩니다. 원문과 추가 요청의 분량을 확인해주세요.',
        ai_source_review_required: '원문의 생략 표시·본문 형식을 확인해야 해 AI 분석을 보류했습니다. 원문은 보존됩니다. 상세 출처에서 본문이 온전히 수집됐는지 확인해주세요.',
        ai_response_review_required: 'AI 응답이 중간에 끝났거나 완전한 결과를 확인하지 못해 자동 재시도를 멈췄습니다. 원문은 보존됩니다. 내용을 확인한 뒤 다시 분석해주세요.',
        retry_limit_reached: 'AI 응답을 받지 못해 재시도를 멈췄습니다. 연결 상태를 확인하고 잠시 후 이어서 진행해주세요.',
        interrupted: '앱이 종료되어 멈춘 작업입니다. 이미 처리한 자료를 보존한 채 이어서 진행할 수 있습니다.',
        shutdown: '앱 종료로 작업이 멈췄습니다. 이어서 진행하면 남은 자료를 처리합니다.',
        busy: '다른 수집 또는 AI 작업이 끝난 뒤 이어서 진행할 수 있습니다.',
        batch_limit: '이전 실행에서 남겨 둔 자료가 있습니다. 이어서 진행하면 남은 자료를 자동으로 계속 처리합니다.',
        analysis_failed: item.can_resume ? '분석을 마치지 못한 자료가 있습니다. AI 연결과 사용 한도를 확인한 뒤 이어서 진행해주세요.' : '분석을 마치지 못한 자료가 있습니다. 이 수집의 결과에서 처리된 자료를 확인해주세요.',
    };
    return notices[reason] || (item.state === 'waiting'
        ? '작업이 잠시 멈췄습니다. 이어서 진행하거나 이 작업을 종료할 수 있습니다.'
        : '작업을 마치지 못했습니다. 처리된 자료는 이 수집의 결과에서 확인할 수 있습니다.');
}

export function sourceProgressText(item) {
    const names={sites:'기관 사이트',bokjiro:'복지로'};
    const states={finished:'확인 완료',busy:'잠시 대기',shutdown:'앱 종료로 멈춤',more_pages:'추가 페이지 확인 중',waiting:'잠시 멈춤'};
    const count=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
    return Object.entries(item.summary?.collection?.sources||{})
        .filter(([source,summary])=>names[source]&&summary&&typeof summary==='object')
        .map(([source,summary])=>{
            const failedPage=source==='bokjiro'&&summary.state==='waiting'&&(summary.stop_reason==='bokjiro_page_failed'||item.error_code==='bokjiro_page_failed');
            const failedSite=source==='sites'&&summary.state==='waiting'&&(summary.stop_reason==='site_collection_failed'||item.error_code==='site_collection_failed');
            const skipped=source==='sites'&&count(summary.skipped_candidates)&&summary.skipped_candidates>0;
            const visionReview=source==='sites'&&count(summary.vision_review_sites)&&summary.vision_review_sites>0;
            const siteReview=source==='sites'&&count(summary.site_review_required)&&summary.site_review_required>0;
            const parts=[`${names[source]}: ${failedPage?'일부 페이지 확인 실패':failedSite?'일부 자료 확인 실패':siteReview&&summary.state==='finished'?'수집 완료 · 일부 사이트 확인 필요':skipped&&summary.state==='finished'?'확인 완료 · 수집하지 못한 자료 있음':visionReview&&summary.state==='finished'?'확인 완료 · 화면 이미지 확인 필요':states[summary.state]||'확인 중'}`];
            if(source==='bokjiro'){
                // 원본 총수와 조건에 맞는 등록 수는 서로 다른 모수이므로 진행률로 나누지 않는다.
                if(count(summary.total_reported))parts.push(`원본 목록 ${summary.total_reported.toLocaleString('ko-KR')}건`);
                if(count(summary.scanned))parts.push(`확인한 원본 ${summary.scanned.toLocaleString('ko-KR')}건`);
                if(count(summary.registered))parts.push(`조건에 맞아 저장 ${summary.registered.toLocaleString('ko-KR')}건`);
            }
            if(source==='sites'&&count(summary.saved_total))parts.push(`누적 저장 후보 ${summary.saved_total.toLocaleString('ko-KR')}건`);
            if(skipped)parts.push(`원문 링크·제목 확인 필요 ${summary.skipped_candidates.toLocaleString('ko-KR')}건`);
            if(siteReview)parts.push(`확인 필요 ${summary.site_review_required.toLocaleString('ko-KR')}개 사이트`);
            if(visionReview)parts.push(`화면 이미지 확인 필요 ${summary.vision_review_sites.toLocaleString('ko-KR')}개 사이트`);
            return parts.join(' · ');
        }).join(' · ');
}

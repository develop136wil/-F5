const PHASE_LABELS = {
    sync: 'Notion 최신 내용 불러오기',
    recompute: '저장된 제안 다시 점검',
    check: '최초·변경 내용 비교',
    audit: '아직 비교하지 않은 정책 점검',
    style: '제안 문장 다듬기',
    embedding: '정책 제목 검색 데이터 정리',
};

export function policyFullCheckConfirmation(plan) {
    if (!plan || ['initial_count', 'pending_count', 'style_count', 'policy_count'].some(key =>
        !Number.isInteger(plan[key]) || plan[key] < 0) || typeof plan.style_configured !== 'boolean') {
        throw new Error('점검 대상 정보를 확인하지 못했습니다. 다시 시도해주세요.');
    }
    if (!plan.initial_count && !plan.style_count && plan.policy_count > 0) return null;
    return [
        '선택한 정책표의 정보를 한 번에 점검할까요?',
        '',
        `예상 대상: 최초 비교 ${plan.initial_count}건 · 저장된 제안 ${plan.pending_count}건 · 문장 다듬기 ${plan.style_count}건`,
        plan.style_configured ? '작성 스타일이 설정되어 있어 필요한 제안의 문장도 다듬습니다.' : '작성 스타일이 설정되지 않아 문장 다듬기는 건너뜁니다.',
        '대상 수는 저장된 자료를 기준으로 한 예상치이며, 정확한 AI 호출 횟수가 아닙니다. Notion에서 새로 불러오거나 출처가 바뀐 정책이 추가될 수 있습니다.',
        '작업이 중단되어도 완료한 내용은 저장됩니다. 같은 버튼으로 남은 작업을 이어서 진행할 수 있습니다.',
        '변경 제안만 준비하며 Notion에는 자동으로 반영하지 않습니다.',
    ].join('\n');
}

export function policyFullCheckPhase(status) {
    return status?.running ? PHASE_LABELS[status.phase] || '정책 점검 준비' : ({
        completed: '점검 완료', partial: '일부 점검 완료', paused: '점검 일시 중지', error: '점검 확인 필요',
    }[status?.state] || '정책 점검 결과');
}

export function policyFullCheckNeedsAttention(status) {
    return !status?.running && ['partial', 'paused', 'error'].includes(status?.state);
}

export function policyFullCheckHasResult(status) {
    return Boolean(status?.running || ['completed', 'partial', 'paused', 'error'].includes(status?.state));
}

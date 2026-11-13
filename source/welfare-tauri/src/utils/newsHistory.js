import {blocksNewWorkflow, canResumeWorkflow} from './newsWorkflowState.js';

export function canClearHistory(item) {
    return item.can_clear_history === true && !item.active
        && (['complete','cancelled','failed'].includes(item.state)
            || (item.state==='waiting'&&item.resume_block_reason==='news_source_scope_changed')) && !canResumeWorkflow(item);
}

export function splitHistory(items, revision) {
    const foreground = items.filter(item => item.active || ['ready','running'].includes(item.state)
        || blocksNewWorkflow(item, revision) || (item.revision === revision && canResumeWorkflow(item)));
    const ids = new Set(foreground.map(item => item.workflow_id));
    return {foreground, history:items.filter(item => !ids.has(item.workflow_id))};
}

// Keep one recent outcome visible after progress moves into collapsed history.
// A newer active/paused run and a different saved condition take precedence.
export function latestTerminalWorkflow(items, revision) {
    if (items.some(item => item.active || ['ready','running'].includes(item.state))) return null;
    const latest = items[0];
    if (!latest || !['complete','cancelled','failed'].includes(latest.state) || canResumeWorkflow(latest)) return null;
    if (Number.isInteger(revision) && latest.revision !== revision) return null;
    return latest;
}

export function historyDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value)) return '날짜 정보 없음';
    const normalized=value.replace(' ','T');
    const date=new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(normalized) ? normalized : normalized+'Z');
    return Number.isNaN(date.getTime()) ? '날짜 정보 없음' : date.toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
}

export const historyConfirmation = '완료·종료된 수집 이력을 목록에서 정리할까요?\n찾은 소식·저장 자료·Notion과 수집 근거는 삭제하지 않습니다. 진행 중이거나 이어서 처리할 작업은 제외합니다.\n오래된 미채택 자료의 실제 삭제는 설정의 7일·30일 보관 기준을 따릅니다.';

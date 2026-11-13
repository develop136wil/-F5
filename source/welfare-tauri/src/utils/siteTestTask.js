const TASK_STATUSES = new Set(['idle', 'running', 'completed', 'failed', 'timed_out', 'cancelled']);

// A transport failure is not a failed site inspection. Only a valid server
// snapshot may finish the task; malformed responses stay eligible for polling.
export function validateSiteTestTask(task, siteId) {
    if (!task || typeof task !== 'object' || Array.isArray(task) ||
        Number(task.site_id) !== Number(siteId) || !TASK_STATUSES.has(task.status) ||
        typeof task.running !== 'boolean' || task.running !== (task.status === 'running') ||
        (task.status !== 'idle' && !task.task_id) ||
        (task.status === 'completed' && (!task.result || typeof task.result !== 'object' || Array.isArray(task.result)))) {
        throw new Error('점검 상태를 다시 확인하고 있습니다.');
    }
    return task;
}

export function siteTestOutcome(task) {
    if (task.status !== 'completed') {
        return { level: 'error', message: task.message || (task.status === 'timed_out'
            ? '사이트 응답이 늦어 점검을 마치지 못했습니다. 잠시 후 다시 확인해주세요.'
            : task.status === 'idle' ? '진행 중인 점검을 찾지 못했습니다. 다시 점검해주세요.'
                : '사이트 점검을 마치지 못했습니다. 잠시 후 다시 확인해주세요.') };
    }
    const data = task.result;
    if (data.registration_status) return data.registration_status === 'basic_verified'
        ? { level: 'success', message: '기본 수집으로 게시글 본문을 확인했습니다.' }
        : { level: 'warning', message: '아직 게시글 본문을 확인하지 못했습니다. 주소는 수집 대상으로 유지됩니다.' };
    if (data.count > 0) return { level: 'success', message: `${data.site || '사이트'}: ${data.count}개 항목 발견!${data.items?.[0]?.title ? `\n대표: ${data.items[0].title}` : ''}` };
    return { level: 'warning', message: `${data.site || '사이트'}: 항목을 찾지 못했습니다. 게시판 주소와 수집 규칙을 확인해주세요.` };
}

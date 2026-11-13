export const POLICY_TASK_LABELS = ['정책 최신화', 'Notion 불러오기', '정책 비교',
    '스타일 제안 생성', '변경 제안 다시 확인', '출처 변경 확인'];

export async function fetchPolicyTaskStatuses(read, signal) {
    const tasks = await read(signal);
    // A failed/incomplete snapshot is not proof that the user's work stopped.
    // Throw so React Query retains its last data and shows the existing warning.
    if (!Array.isArray(tasks) || tasks.length !== POLICY_TASK_LABELS.length ||
        tasks.some((task, index) => !task || task.label !== POLICY_TASK_LABELS[index] ||
            typeof task.running !== 'boolean')) {
        throw new Error('정책 진행 상태를 확인하지 못했습니다.');
    }
    return tasks;
}

export function policyTaskPresentation(tasks = []) {
    const task = tasks.find(task => task.running);
    if (!task) return null;
    const total = Number.isFinite(task.total) && task.total > 0 ? task.total : 0;
    const checked = Number.isFinite(task.checked) ? Math.max(0, Math.min(total, task.checked)) : 0;
    return { ...task, total, checked, percent: total ? Math.floor(checked / total * 100) : null };
}

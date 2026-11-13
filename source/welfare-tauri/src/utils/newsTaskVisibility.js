const key = profileId => `news-hidden-tasks:v1:${profileId}`;
const clean = ids => [...new Set(Array.isArray(ids) ? ids.filter(id => typeof id === 'string' && id.length > 0 && id.length <= 100) : [])].slice(-100);

// Presentation preference only: never remove tasks from execution/locking state.
export function readHiddenTasks(profileId, storage) {
    try { return clean(JSON.parse((storage ?? globalThis.localStorage).getItem(key(profileId)) || '[]')); }
    catch { return []; }
}

export function writeHiddenTasks(profileId, ids, storage) {
    try { (storage ?? globalThis.localStorage).setItem(key(profileId), JSON.stringify(clean(ids))); return true; }
    catch { return false; }
}

export const hideTaskConfirmation = '이 작업을 화면에서 숨길까요?\n진행 중인 작업은 계속 실행되며 AI 사용량도 발생할 수 있습니다. 대기 중인 작업은 자동 재개되지 않습니다.\n작업과 자료는 삭제되지 않으며, ‘숨긴 작업’에서 다시 볼 수 있습니다. 실행을 멈추려면 ‘작업 중지’를 사용하세요.';

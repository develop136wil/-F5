export const featureSetupSteps = mode => ['google', 'signup', 'notion', 'template', mode === 'policy' ? 'policy' : 'news'];

export const featureSetupItems = mode => featureSetupSteps(mode).map((key, index) => ({
    key, label: ['Gemini 설정', 'Notion 가입', 'Notion API', '템플릿 복제', '주소 등록'][index],
}));

// An explicit edit must expose the form even when the saved connection is healthy.
export const isConnectionFormVisible = (ready, editing, key) => !ready || editing === key;

export function firstIncompleteSetupStep(mode, done) {
    return featureSetupSteps(mode).find(key => !done[key]) ?? null;
}

export function nextSetupStep(mode, completedKey, done) {
    const steps = featureSetupSteps(mode);
    return steps.slice(steps.indexOf(completedKey) + 1).find(key => !done[key]) ?? null;
}

export const buildNotionSetupGuideSteps = ({
    templateAvailable = false,
    tokenConfigured = false,
    configuredPolicyDbCount = 0,
    notionReady = false,
} = {}) => {
    const dbCount = Math.max(0, Number(configuredPolicyDbCount) || 0);

    return [
        {
            key: 'template',
            line: '표준 정책 템플릿을 내 워크스페이스에 복제 (권장)',
            // 공개 링크는 앱의 준비 상태일 뿐, 사용자가 복제했다는 증거가 아니다.
            done: false,
            note: templateAvailable ? '템플릿 사용 가능' : '공개 준비 중',
        },
        {
            key: 'integration',
            line: '내부 연결을 만들고 Notion 연결 토큰 저장',
            done: Boolean(tokenConfigured),
        },
        {
            key: 'database',
            line: `사용할 정책표에 복지F5 연결을 추가하고 주소 입력 (${dbCount}개 등록)`,
            done: dbCount >= 1,
        },
        {
            key: 'verification',
            line: '복지F5에서 접근 권한과 15개 필수 속성 확인',
            done: Boolean(notionReady),
        },
    ];
};

export const buildOnboardingProgress = (done = {}) => {
    const collectionKeys = ['google', 'target', 'sites'];
    const policyKeys = ['google', 'notion', 'policy'];
    const newsKeys = ['notion', 'news'];
    const completed = keys => keys.filter(key => Boolean(done[key])).length;

    return {
        collectionCompleted: completed(collectionKeys),
        collectionTotal: collectionKeys.length,
        collectionReady: collectionKeys.every(key => Boolean(done[key])),
        policyCompleted: completed(policyKeys),
        policyTotal: policyKeys.length,
        policyReady: policyKeys.every(key => Boolean(done[key])),
        newsCompleted: completed(newsKeys),
        newsTotal: newsKeys.length,
        newsReady: newsKeys.every(key => Boolean(done[key])),
    };
};

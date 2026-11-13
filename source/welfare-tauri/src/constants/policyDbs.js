// 과거 설치본의 고정 5개 보관함 설정을 읽기 위한 호환 기본값이다.
// 새 설정 화면에서는 사용자가 필요한 만큼 보관함 연결을 추가·해제한다.
export const POLICY_DBS = [
    { key: 'medical', name: '의료·재활', category: '의료재활' },
    { key: 'education', name: '교육·보육', category: '교육보육' },
    { key: 'care', name: '돌봄·양육', category: '돌봄양육' },
    { key: 'family', name: '가족지원', category: '가족지원' },
    { key: 'living', name: '생활지원', category: '복지' },
];

const clean = value => typeof value === 'string' ? value.trim() : '';

export const createPolicyDbDraft = (index = 0) => ({
    key: `policy_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
    name: `정책표 ${index + 1}`,
    category: '',
    data_source_id: '',
});

export const parsePolicyDbs = raw => {
    try {
        const values = typeof raw === 'string' ? JSON.parse(raw || '[]') : raw;
        if (!Array.isArray(values)) return [];
        const used = new Set();
        return values.flatMap((item, index) => {
            const id = clean(item?.data_source_id);
            if (!id) return [];
            let key = clean(item?.key) || `policy_${index + 1}`;
            while (used.has(key)) key = `${key}_${index + 1}`;
            used.add(key);
            return [{
                key,
                name: clean(item?.name) || `정책표 ${index + 1}`,
                category: clean(item?.category),
                data_source_id: id,
            }];
        });
    } catch {
        return [];
    }
};

export const policyDbPayload = rows => parsePolicyDbs(Array.isArray(rows) ? rows : []);

export const applyVerifiedPolicyTitles = (rows, verifiedItems = []) => rows.map(row => {
    const verified = verifiedItems.find(item => item?.key === row.key);
    return verified?.title ? { ...row, name: clean(verified.title) || row.name } : row;
});

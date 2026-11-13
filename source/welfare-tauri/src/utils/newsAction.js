// One action owns selection from confirmation through API completion.
export function createNewsActionGate(onBusyChange) {
    let active = false;
    return {
        async run(task) {
            if (active) return false;
            active = true;
            onBusyChange(true);
            try { await task(); return true; }
            finally { active = false; onBusyChange(false); }
        },
    };
}

export function snapshotNewsSelection(items, ids) {
    const wanted = new Set(ids);
    const rows = items.filter(item => wanted.has(item.id));
    return {
        ids: rows.map(item => item.id),
        reviewCount: rows.filter(item => item.review_required || item.attachment_review_required || item.input_review_required).length,
        signature: JSON.stringify(rows.slice().sort((a, b) => String(a.id).localeCompare(String(b.id)))),
    };
}

export function isNewsSelectionCurrent(snapshot, items) {
    return snapshot.signature === snapshotNewsSelection(items, snapshot.ids).signature;
}

export function newsActionConfirmation(kind, snapshot) {
    const count = snapshot.ids.length;
    if (kind === 'dismiss') return {
        title: `${count}건을 후보에서 제외할까요?`, confirmLabel: '후보에서 제외',
        description: '제외한 항목은 이 후보 목록에 다시 표시되지 않습니다. 기존 수집 자료와 Notion에 등록된 내용은 삭제하지 않습니다.',
    };
    if (kind === 'retry') return {
        title: 'Notion에서 등록 결과를 확인하셨나요?', confirmLabel: '미등록 확인 · 재시도 허용',
        description: `선택한 ${count}건이 Notion에 이미 등록돼 있다면 다시 등록할 때 중복됩니다. Notion에 생성되지 않은 항목만 선택했는지 확인해주세요.`,
    };
    if (!snapshot.reviewCount) return null;
    return {
        title: kind === 'html' ? '소식 모음 파일로 저장하기 전에 확인해주세요' : 'Notion에 등록하기 전에 확인해주세요',
        confirmLabel: kind === 'html' ? '확인했어요 · 파일 저장' : '확인했어요 · Notion 등록',
        description: `선택한 ${count}건 중 ${snapshot.reviewCount}건은 대상·기간·근거 중 확인이 필요한 내용이 있습니다. 본문·첨부파일에서 빠진 내용이나 신청 조건이 없는지 해당 부분을 원문과 비교해주세요.`,
        note: kind === 'html' ? '원문 확인 안내는 저장한 파일에도 남습니다. 파일 생성에는 AI를 사용하지 않습니다.' : '확인하지 않았다면 취소한 뒤 목록의 원문 링크를 열어보세요.',
    };
}

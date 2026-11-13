export async function applyWithSourceConfirmation(field, policyTitle, confirm, apply) {
    if (field.requires_individual_review) {
        const accepted = confirm(
            `${policyTitle}\n${field.field}\n\n` +
            '이 제안은 내용 정확성 검증이 끝나지 않았습니다.\n' +
            '원문에서 다음 내용을 대조하셨나요?\n' +
            '• 대상별 혜택과 금액\n• 필수 조건과 제외 조건\n• 빠진 조건이 없는지\n\n' +
            '확인하면 이 항목만 Notion에 반영합니다. 아직 대조하지 않았다면 취소해주세요.'
        );
        if (!accepted) return false;
    }
    await apply();
    return true;
}

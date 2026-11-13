export function newsRetentionDays(value) {
    if (typeof value !== 'number' || ![0, 7, 30].includes(value)) {
        throw Error('기록 보관 기간을 다시 확인해주세요.');
    }
    return value;
}

export function newsRetentionConfirmation(value) {
    const days = newsRetentionDays(value);
    if (!days) return null;
    return `${days}일이 지난 미채택 수집 기록을 자동으로 정리할까요?\n\n` +
        '앱 시작 또는 수집 완료 후 하루 1회 정리합니다.\n' +
        '기존 분석 실패·보류·제외 기록의 본문과 분석 결과도 정리됩니다. 정리한 기록은 앱에서 되돌릴 수 없습니다.\n' +
        '찾은 소식, 저장한 자료, Notion 원본과 진행 중인 작업은 유지됩니다.';
}

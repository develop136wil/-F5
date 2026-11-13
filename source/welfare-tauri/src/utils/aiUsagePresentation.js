export function aiUsagePresentation(query = {}) {
    const data = query.data;
    if (query.isError) return {title: 'AI 상태 확인 필요', note: '상태를 불러오지 못했습니다. 잠시 후 다시 확인해주세요.', tone: 'warning', count: null};
    if (!data) return {title: 'AI 상태 확인 중', note: '', tone: 'neutral', count: null};
    const models = Object.entries(data.models || {}).filter(([key]) => key !== '*').map(([, value]) => value);
    const count = Number.isInteger(data.recorded_requests_today) && data.recorded_requests_today >= 0 ? data.recorded_requests_today : null;
    const base = {count, tone: 'neutral', note: ''};
    if (data.state_warning) return {...base, count: null, tone: 'warning', title: 'AI 사용 기록 확인 필요', note: `${data.state_warning} 기록 복구 또는 다음 한도 초기화 후 작업을 다시 시작해주세요.`};
    // A wildcard hold may also protect an unreadable ledger. Do not hide it or
    // incorrectly present it as a Google-confirmed per-model daily quota.
    if (data.models?.['*']?.daily_exhausted) return {...base, count: null, tone: 'warning', title: 'AI 사용 상태 확인 필요', note: 'AI 사용 한도 또는 사용 기록을 확인할 때까지 요청을 보류합니다. 작업은 보관됩니다. 잠시 후 상태를 다시 확인해주세요.'};
    // Older servers/cached responses may still contain the retired app daily
    // budget flag. Only provider-confirmed limits should trigger quota notices.
    if (models.some(model => model.daily_exhausted)) return {...base, tone: 'warning', title: '일부 AI 모델의 하루 한도에 도달했습니다', note: '한도 초기화 후 필요한 작업을 다시 시작해주세요. 새 소식 대기 자료는 다음 수집 때 이어서 분석합니다. 이 안내만으로 자동 재개되지는 않습니다.'};
    const seconds = Math.ceil(Math.max(0, ...models.map(model => Number(model.cooldown_seconds) || 0)));
    const waiting = models.reduce((sum, model) => sum + (Number(model.waiting) || 0), 0);
    if (waiting > 0) return {...base, tone: 'warning', title: 'AI 요청을 순서대로 처리하고 있습니다', note: `${waiting}개 요청 대기 중.${seconds ? ` 마지막 확인 기준 약 ${seconds}초 뒤 재시도할 수 있습니다.` : ''} 실행 중인 작업이 유지되면 차례대로 시도합니다. 완료 예정 시간은 아닙니다.`};
    if (seconds > 0) return {...base, tone: 'warning', title: 'AI 호출을 잠시 쉬고 있습니다', note: `마지막 확인 기준 약 ${seconds}초 뒤 다음 요청이 가능합니다. 현재 대기 중인 요청은 없으며, 작업을 자동으로 시작하지 않습니다.`};
    if (data.busy) return {...base, title: `AI 작업 중 · ${data.job_label || '분석 중'}`, note: '다른 AI 작업은 완료 후 시작할 수 있습니다. AI를 쓰지 않는 화면은 계속 이용할 수 있습니다.'};
    return {...base, title: 'AI 요청 대기 없음'};
}

import {formatSaveResult} from './saveResult.js';

const count = value => Number.isSafeInteger(value) && value > 0 ? value : 0;

// Completion is not proof that analysis succeeded. Keep older/partial status
// responses honest even when the producer still labels the run completed.
export function scrapeCompletionNotice(status = {}) {
    if (status.is_running) return null;
    const code = status.status_code;
    const savings = status.ai_savings || {};
    const failed = count(savings.analysis_failed);
    const deferred = count(savings.analysis_deferred) || count(savings.quota_skipped);
    if (code === 'partial_failure' || (code === 'completed' && (failed || deferred))) {
        const counts = [failed && `분석 실패 ${failed}건`, deferred && `분석 대기 ${deferred}건`].filter(Boolean).join(' · ');
        return {tone: 'warning', message: code === 'partial_failure' && status.message
            ? status.message
            : `일부 자료의 분석을 마치지 못했습니다.${counts ? ` ${counts}.` : ''} 완료된 소식만 목록에 표시합니다.`};
    }
    if (code === 'stopped') return {tone: 'warning', message: '새 소식 찾기가 중지되었습니다.'};
    if (code === 'error') return {tone: 'error', message: `새 소식 찾기를 마치지 못했습니다.${status.message ? ` ${status.message}` : ''}`};
    if (code !== 'completed') return null;
    const saved = formatSaveResult(status.save_result);
    return {tone: 'success', message: saved ? `새 소식 처리를 마쳤습니다. ${saved}` : '새 소식 처리를 마쳤습니다.'};
}

/**
 * 임베딩 백필(의미 검색 데이터 생성) 결과 판정 — Settings.jsx와 Search.jsx가 동일한
 * 기준으로 성공/부분실패/전부실패/한도소진을 안내하도록 공통 함수로 분리했다.
 *
 * (2026-08-05, GPT Codex 리뷰) 예전엔 양쪽 화면이 각자 `embedded === 0`이면 "이미
 * 최신"/"생성 완료"로 표시해, 이번 배치가 전부 실패한 경우(예: 한도 소진 도중 처음
 * 걸림)에도 성공한 것처럼 보였다. 백엔드가 반환하는 `failed`/`unavailable_reason`을
 * 실제로 사용해 구분하고, 두 화면이 서로 다른 기준으로 어긋나지 않도록 이 함수 하나만
 * 쓰게 한다.
 *
 * @param {number} totalEmbedded - 이번 세션(여러 배치 반복) 동안 누적 성공 건수
 * @param {{embedded:number, failed:number, remaining:number, unavailable_reason?:string|null}} lastResult
 *   - 반복 루프의 마지막 배치 응답
 * @returns {{status: 'success'|'partial'|'failed'|'quota', message: string}}
 */
export function summarizeBackfillOutcome(totalEmbedded, lastResult) {
    const { failed = 0, remaining = 0, unavailable_reason, held_for_input_size = 0 } = lastResult || {};

    if (remaining === 0) {
        return {
            status: 'success',
            message: totalEmbedded > 0
                ? `의미 검색 데이터 ${totalEmbedded}건 생성 완료`
                : '이미 최신 상태입니다',
        };
    }

    if (unavailable_reason === 'daily_quota_exhausted') {
        return {
            status: 'quota',
            message: `임베딩 일일 한도 소진으로 ${remaining}건이 남았습니다 (태평양 자정 이후 재개, 지금까지 ${totalEmbedded}건 생성).`,
        };
    }

    if (held_for_input_size > 0 && failed === 0 && !unavailable_reason) {
        return {
            status: 'partial',
            message: `${totalEmbedded}건 생성, 분량이 큰 ${held_for_input_size}건은 의미 검색용 AI 처리를 보류했습니다. 원문은 유지되며 단어 검색을 사용할 수 있습니다 (${remaining}건 남음).`,
        };
    }

    if (totalEmbedded > 0 && failed > 0) {
        return {
            status: 'partial',
            message: `${totalEmbedded}건 생성, ${failed}건 실패, ${remaining}건 남음 — 잠시 후 다시 시도해주세요.`,
        };
    }

    if (totalEmbedded === 0 && failed > 0) {
        return {
            status: 'failed',
            message: `의미 검색 데이터를 생성하지 못했습니다 (${remaining}건 남음). 잠시 후 다시 시도해주세요.`,
        };
    }

    // failed=0인데 remaining>0인 흔치 않은 경우(예: guard 상한 도달) — 안전한 기본값
    return {
        status: 'partial',
        message: `${totalEmbedded}건 생성, ${remaining}건 남음 — 다시 실행하면 이어서 생성됩니다.`,
    };
}

// Continue a bounded scan past length-held rows, never past a failed AI call.
// Undefined cursor fields keep compatibility with the prior backend contract.
export function nextBackfillCursor(result, current = 0) {
    if (!result || !Number.isSafeInteger(current) || current < 0) return null;
    const { embedded, remaining, failed = 0 } = result;
    if (![embedded, remaining, failed].every(n => Number.isSafeInteger(n) && n >= 0)
        || remaining === 0 || failed > 0 || result.unavailable_reason || result.scan_complete === true) return null;
    if (Object.hasOwn(result, 'next_cursor')) {
        return Number.isSafeInteger(result.next_cursor) && result.next_cursor > current
            && (embedded > 0 || (result.held_for_input_size > 0 && result.scanned > 0))
            ? result.next_cursor : null;
    }
    return embedded > 0 ? current : null;
}

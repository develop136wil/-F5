/**
 * Axios 에러 또는 일반 JS 에러에서 사용자 친화적인 메시지를 추출합니다.
 * onError 콜백 전반에 흩어진 중복 패턴을 통일합니다.
 *
 * @param {Error|Object} error - Axios 에러 또는 일반 Error 객체
 * @param {string} [fallback='알 수 없는 오류가 발생했습니다.'] - 메시지를 추출할 수 없을 때 반환할 기본값
 * @returns {string} 사용자에게 표시할 에러 메시지
 *
 * @example
 * onError: (error) => toast.error(getErrorMessage(error))
 */
export const getErrorMessage = (error, fallback = '알 수 없는 오류가 발생했습니다.') => {
    if (!error) return fallback;

    // Axios 에러 — API 응답 body에 detail 필드 (FastAPI 표준 ValidationError 포함)
    const detail = error.response?.data?.detail;
    if (detail) {
        if (Array.isArray(detail)) {
            // FastAPI ValidationError 배열: [{loc, msg, type}, ...]
            return detail.map(e => e.msg || String(e)).join(', ');
        }
        if (typeof detail === 'string') return detail;
        if (typeof detail === 'object') return JSON.stringify(detail);
    }

    // Axios 에러 — API 응답 body에 message 필드
    if (error.response?.data?.message) return error.response.data.message;

    // 응답이 없는 네트워크/타임아웃 에러 (axios가 영문 메시지를 그대로 던짐)
    if (error.code === 'ECONNABORTED') return '요청 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.';
    if (!error.response && (error.message === 'Network Error' || error.code === 'ERR_NETWORK')) {
        return '서버에 연결할 수 없습니다. 앱을 다시 시작해보세요.';
    }

    // 일반 JavaScript Error
    if (error.message) return error.message;

    return fallback;
};

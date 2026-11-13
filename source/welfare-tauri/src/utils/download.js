/**
 * Blob/ArrayBuffer 데이터를 파일로 다운로드합니다.
 * welfare.js 전반에 흩어진 중복 다운로드 패턴을 통일합니다.
 *
 * @param {BlobPart} data - Blob 또는 ArrayBuffer 데이터
 * @param {string} filename - 저장할 파일명
 * @param {string} mimeType - MIME 타입 (기본: 'application/octet-stream')
 */
export const downloadBlob = (data, filename, mimeType = 'application/octet-stream') => {
    const url = window.URL.createObjectURL(new Blob([data], { type: mimeType }));
    const link = document.createElement('a');
    link.href = url;
    link.setAttribute('download', filename);
    try {
        document.body.appendChild(link);
        link.click();
    } catch (error) {
        window.URL.revokeObjectURL(url);
        throw error;
    } finally {
        link.remove();
    }
    // WebView may read the Blob after click() returns. Releasing it immediately
    // can race the download; bound its lifetime without retaining it indefinitely.
    setTimeout(() => window.URL.revokeObjectURL(url), 30_000);
};

/**
 * 오늘 날짜를 ISO 형식 (YYYY-MM-DD) 문자열로 반환합니다.
 * 파일명 suffix 생성에 사용합니다.
 *
 * @returns {string} 예: '2026-02-25'
 */
export const todayStr = () => new Date().toISOString().slice(0, 10);

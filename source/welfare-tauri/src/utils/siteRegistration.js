// This is an advisory check, not a board detector. Query-based boards and home
// pages that really are post lists must remain registerable after confirmation.
export function isLikelyHomepageUrl(value) {
    try {
        const url = new URL(value);
        if (!['http:', 'https:'].includes(url.protocol) || url.search) return false;
        return /^(?:\/?|\/(?:index|main|default)(?:\.(?:html?|jsp|do|aspx?|php))?\/?)$/i.test(url.pathname);
    } catch {
        return false;
    }
}

export function getHomepageRegistrationWarning(urls) {
    const homepages = urls.filter(isLikelyHomepageUrl);
    if (!homepages.length) return '';
    const examples = homepages.slice(0, 3).join('\n');
    return `대표 홈페이지로 보이는 주소 ${homepages.length}개가 있습니다.\n${examples}${homepages.length > 3 ? '\n…' : ''}\n\n공지사항·모집 안내가 여러 건 나열된 게시판 목록을 연 뒤 그 주소를 복사해주세요. 대표 홈이나 개별 게시글 주소는 새 소식 수집에 적합하지 않을 수 있습니다.\n\n이 주소의 화면 자체가 게시판 목록인 것을 확인한 경우에만 계속 등록해주세요. 게시글을 확인하지 못하면 ‘수집 확인 필요’로 저장됩니다. 계속할까요?`;
}

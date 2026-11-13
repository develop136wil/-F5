// 일부 구형 게시판은 href가 실제 URL이 아니라 'javascript:go_view(...)' 같은 onclick
// 트리거를 그대로 노출한다 — 클릭해도 아무 데도 안 열리고, React가 향후 버전에서
// 이런 javascript: URL을 아예 차단할 예정이라 미리 걸러 링크 자체를 숨긴다.
export const isRealLink = (link) => typeof link === 'string' && /^https?:\/\//i.test(link);

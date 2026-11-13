/**
 * React Query 쿼리 키 중앙 관리.
 * 키 불일치로 인한 캐시 버그를 방지하고 invalidateQueries 호출을 통일합니다.
 *
 * 사용 예:
 *   queryKey: queryKeys.summaryStats
 *   queryClient.invalidateQueries({ queryKey: queryKeys.sites })
 */
export const queryKeys = {
    // 대시보드
    summaryStats: ['summaryStats'],
    logs: ['logs'],
    recentItems: (limit) => ['recentItems', limit],

    // 사이트 관리
    sites: ['sites'],
    siteTestTask: ['siteTestTask'],
    batchStatus: ['batchStatus'],

    // 검색
    search: (query, category, page, activeOnly = false) => ['search', query, category, page, activeOnly],

    // 리포트 (쿼리 없음, 버튼 클릭 시 즉시 호출)

    // 설정
    config: ['config'],
    keywords: ['keywords'],
    excludedKeywords: ['excludedKeywords'],
    health: ['health'],
    logFiles: ['logFiles'],

    // 항목 상세
    itemDetail: (id) => ['itemDetail', id],

    // 마감 임박
    expiringItems: (days) => ['expiringItems', days],

    // 트렌드 차트
    trends: (days) => ['trends', days],

    // 유사 항목 추천
    similarItems: (id) => ['similarItems', id],

    // 변경 내역 스냅샷
    itemSnapshots: (id) => ['itemSnapshots', id],
    itemDiff: (id, snapshotId) => ['itemDiff', id, snapshotId],

    // 사이트 건강 대시보드
    sitesHealth: ['sitesHealth'],
    siteHealth: (siteId) => ['siteHealth', siteId],

    // AI 분석 백로그 (2026-08-06)
    aiBacklogStatus: ['aiBacklogStatus'],
    notionCandidates: ['notionCandidates'],
    newsFeed: ['newsFeed'],
};

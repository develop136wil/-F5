"""
history_manager 파사드.

원래 하나의 거대 모듈(1300줄+)이었던 것을 도메인별 서브모듈로 분리했다.
기존 호출부(`import history_manager` → `history_manager.save_data(...)` 등)가
전혀 바뀔 필요 없도록, 각 서브모듈의 공개 함수를 이 파일에서 그대로 재노출한다.

- common: 제목 정규화/유사도, ai_data 파싱 (다른 서브모듈이 공유하는 헬퍼)
- items: 항목 CRUD, 저장(중복제거·변경감지), 검색, 내보내기
- embeddings: AI 캐시 조회, 임베딩(의미 검색/중복 제거)
- health: 사이트별 헬스체크, 스크랩 실행 이력, 대시보드 집계
- stats: 요약/트렌드 통계, 키워드 추천, 마감 임박 항목
- snapshots: 변경 이력 스냅샷, 항목 diff
- maintenance: 보존기간 정리, 자동 유지보수
"""
from .common import _parse_ai_data

from .items import (
    load_history,
    load_data,
    save_data,
    is_new,
    search_items,
    get_recent_items,
    get_items_by_links,
    get_item_by_id,
    export_data,
    count_ai_backlog,
    get_ai_backlog,
    count_ai_permanently_excluded,
)

from .embeddings import (
    find_cached_ai_data_by_hash,
    save_embedding,
    get_items_missing_embedding,
    count_embeddings,
    get_embedding_index,
    semantic_search,
    hybrid_search,
    get_daily_ai_count,
    get_similar_items,
)

from .health import (
    check_link_health,
    get_sites_last_scraped,
    record_site_scrape,
    get_health_stats,
    record_scrape_run,
    get_site_health_dashboard,
    get_site_scrape_history,
)

from .stats import (
    get_summary_stats,
    get_trend_stats,
    get_keyword_suggestions,
    get_expiring_items,
)

from .snapshots import (
    get_recently_changed_items,
    get_item_snapshots,
    get_item_diff,
)

from .maintenance import (
    cleanup_data,
    run_maintenance,
    archive_expired_items,
)

__all__ = [
    "_parse_ai_data",
    "load_history", "load_data", "save_data", "is_new", "search_items",
    "get_recent_items", "get_items_by_links", "get_item_by_id", "export_data",
    "count_ai_backlog", "get_ai_backlog", "count_ai_permanently_excluded",
    "find_cached_ai_data_by_hash", "save_embedding", "get_items_missing_embedding",
    "count_embeddings", "get_embedding_index", "semantic_search", "hybrid_search", "get_daily_ai_count",
    "get_similar_items",
    "check_link_health", "get_sites_last_scraped", "record_site_scrape",
    "get_health_stats", "record_scrape_run", "get_site_health_dashboard",
    "get_site_scrape_history",
    "get_summary_stats", "get_trend_stats", "get_keyword_suggestions", "get_expiring_items",
    "get_recently_changed_items", "get_item_snapshots", "get_item_diff",
    "cleanup_data", "run_maintenance", "archive_expired_items",
]

"""항목 CRUD, 저장(중복제거·변경감지 포함), 검색, 내보내기."""
import os
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
import database
import utils
from services.logger import setup_logger
from services import search_dates
from .common import _parse_ai_data, _normalize_title, _is_similar_title, _strip_blob
from .snapshots import _save_snapshot

logger = setup_logger(__name__)

def load_history() -> List[str]:
    """
    Returns a list of links from welfare_items table.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT link FROM welfare_items")
            rows = cursor.fetchall()
            return [row['link'] for row in rows]
    except Exception as e:
        logger.error(f"DB Load History Error: {e}")
        raise


def load_data(limit: int = 0) -> List[Dict[str, Any]]:
    """
    Returns welfare items as dictionaries.
    limit > 0이면 최신순으로 해당 건수만 반환. 0이면 전체 반환(레거시).
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            if limit > 0:
                cursor.execute("SELECT * FROM welfare_items ORDER BY scraped_at DESC LIMIT ?", (limit,))
            else:
                cursor.execute("SELECT * FROM welfare_items ORDER BY scraped_at DESC")
            rows = cursor.fetchall()
            items = []
            for row in rows:
                item = _strip_blob(dict(row))
                item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                items.append(item)
            return items
    except Exception as e:
        logger.error(f"DB Load Data Error: {e}")
        raise


def save_data(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """항목을 한 트랜잭션으로 저장하고 실제 저장 결과를 반환한다.

    DB 조회·스냅샷·UPSERT 중 하나라도 실패하면 전체 배치를 롤백하고 예외를
    호출자에게 전달한다. 호출자는 이 결과를 기준으로 완료 건수와 알림을 계산한다.
    """
    if not items:
        return {"requested": 0, "saved": 0, "skipped": 0,
                "saved_links": [], "skipped_links": []}

    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Pre-fetch existing items to check for changes (URL match)
            links = [item['link'] for item in items if item.get('link')]
            existing_map = {}
            if links:
                placeholders = ','.join(['?'] * len(links))
                from services.news_result_import import MANUAL_NEWS_SQL
                cursor.execute(f"SELECT link, deadline, title, last_updated, change_log, page_modified_date, embedding, embedding_recipe, content_hash, support_detail, ai_skip_reason, ai_last_attempt_at, notion_page_id, notion_dismissed, {MANUAL_NEWS_SQL} AS manual_news FROM welfare_items WHERE link IN ({placeholders})", links)
                existing_map = {row['link']: dict(row) for row in cursor.fetchall()}
            
            # Pre-fetch for Intelligent Deduplication (Title + Site Name match)
            # We fetch recent items from the same sites to check for duplicates
            site_names = list(set(item['site_name'] for item in items if item.get('site_name')))
            existing_titles_map = {} # (site_name, title) -> item
            # site_name -> [(normalized_title, row), ...] (유사도 비교용)
            existing_normalized_map: Dict[str, list] = {}
            if site_names:
                site_placeholders = ','.join(['?'] * len(site_names))
                # 중복 체크 기간: 기본 90일, DEDUPE_WINDOW_DAYS 환경변수로 조정 가능
                dedupe_days = max(1, min(int(os.getenv('DEDUPE_WINDOW_DAYS', '90')), 365))
                cursor.execute(f"""
                    SELECT site_name, title, link, scraped_at FROM welfare_items
                    WHERE site_name IN ({site_placeholders})
                    AND scraped_at > date('now', ?)
                    ORDER BY scraped_at DESC LIMIT 5000
                """, site_names + [f"-{dedupe_days} days"])
                for row in cursor.fetchall():
                    r = dict(row)
                    existing_titles_map[(r['site_name'], r['title'])] = r
                    sn = r['site_name']
                    if sn not in existing_normalized_map:
                        existing_normalized_map[sn] = []
                    existing_normalized_map[sn].append((_normalize_title(r['title']), r))

            count = 0
            skipped = 0
            saved_links = []
            saved_link_set = set()
            skipped_links = []
            for item in items:
                # Explicitly curated target results are snapshots, not inputs for
                # the legacy infant collector to silently reclassify/overwrite.
                if existing_map.get(item.get('link'), {}).get('manual_news'):
                    skipped += 1
                    skipped_links.append(item['link'])
                    continue
                # 0. Date Normalization
                # 빈/공백 마감일은 날짜가 아니다. 기존 미상 설명은 보존하되 빈값은 NULL 저장.
                if isinstance(item.get('deadline'), str) and not item['deadline'].strip():
                    item['deadline'] = None
                if item.get('deadline'):
                    normalized = utils.normalize_date(item['deadline'])
                    if normalized:
                        item['deadline'] = normalized
                
                if item.get('event_date'):
                    normalized = utils.normalize_date(item['event_date'])
                    if normalized:
                        item['event_date'] = normalized

                # 1. Intelligent Deduplication
                # (재공고), [마감연장] 등 접두사 제거 후 유사도(90%↑) 비교
                if item.get('link') not in existing_map:
                    norm_new = _normalize_title(item.get('title', ''))
                    dup = existing_titles_map.get((item.get('site_name'), item.get('title')))
                    if not dup:
                        # 정규화된 제목 정확 매칭
                        for (sn, orig_t), row in existing_titles_map.items():
                            if sn == item.get('site_name') and _normalize_title(orig_t) == norm_new:
                                dup = row
                                break
                    if not dup:
                        # 유사도 기반 퍼지 매칭
                        for norm_t, row in existing_normalized_map.get(item.get('site_name', ''), []):
                            if _is_similar_title(norm_new, norm_t):
                                dup = row
                                break
                    if dup:
                        logger.info(f"🚫 유사 중복 스킵: '{item['title']}' ≈ '{dup['title']}'")
                        skipped += 1
                        if item.get('link'):
                            skipped_links.append(item['link'])
                        continue

                # Diff Logic (URL Match)
                existing = existing_map.get(item.get('link'))
                if existing:
                    changes = []
                    # Check Deadline Change
                    old_deadline = existing.get('deadline')
                    new_deadline = item.get('deadline')
                    if old_deadline != new_deadline and new_deadline:
                        changes.append(f"마감일 변경: {old_deadline} -> {new_deadline}")
                    
                    # Check Title Change (Minor)
                    if existing.get('title') != item.get('title'):
                        changes.append("제목 변경")

                    # Check page_modified_date change
                    old_page_mod = existing.get('page_modified_date')
                    new_page_mod = item.get('page_modified_date')
                    if new_page_mod and old_page_mod != new_page_mod:
                        changes.append(f"페이지 수정일 갱신: {old_page_mod or '없음'} → {new_page_mod}")

                    # 정책/내용 변경 감지: 소스 페이지 본문이 바뀌면 content_hash가 달라짐
                    # (지원금액·자격·신청방법 등 실질 변경의 신뢰 신호 → Notion 수정 필요 알림용)
                    old_hash = existing.get('content_hash')
                    new_hash = item.get('content_hash')
                    if old_hash and new_hash and old_hash != new_hash:
                        changes.append("📝 내용 변경 (지원내용·자격 등 확인 필요)")
                        item['content_changed'] = True

                    if changes:
                        now_iso = datetime.now().isoformat()
                        item['last_updated'] = now_iso
                        old_log = existing.get('change_log') or ""
                        new_log_entry = f"[{now_iso[:10]}] {', '.join(changes)}"
                        item['change_log'] = (old_log + "\n" + new_log_entry).strip()
                        logger.info(f"♻️ Update detected for {item['title']}: {changes}")
                        # 스냅샷 저장: 업데이트 전 기존 상태를 보존
                        _save_snapshot(cursor, item['link'], ', '.join(changes))
                    else:
                        # Preserve existing change info
                        item['last_updated'] = existing.get('last_updated')
                        item['change_log'] = existing.get('change_log')
                    # 임베딩 보존: 이번 수집에서 재계산하지 않았다면 기존 벡터 유지.
                    # UPSERT가 해당 컬럼도 갱신하므로 기존값을 명시적으로 전달한다.
                    if not item.get('embedding_blob') and existing.get('embedding'):
                        item['embedding_blob'] = existing.get('embedding')
                        item['embedding_recipe'] = existing.get('embedding_recipe')
                    # ai_skip_reason/ai_last_attempt_at 보존: 이번 저장이 AI 백로그와 무관한
                    # 경로(예: 최종수정일 재확인만)라면 기존 백로그 상태를 지우지 않는다.
                    if 'ai_skip_reason' not in item:
                        item['ai_skip_reason'] = existing.get('ai_skip_reason')
                    if 'ai_last_attempt_at' not in item:
                        item['ai_last_attempt_at'] = existing.get('ai_last_attempt_at')
                    # (2026-08-07, GPT Codex 전수조사로 발견) 과거 INSERT OR REPLACE 시절에는
                    # notion_page_id/notion_dismissed가 저장 목록에 없어 60일 재확인·내용갱신으로 기존
                    # 항목이 재저장될 때마다 조용히 NULL/0으로 초기화되고 있었다 — 이미 Notion에
                    # 등록한 항목이 새소식 후보로 재등장하거나, 사용자가 제외한 항목이 다시
                    # 나타날 수 있다(실측: 운영 DB에 등록 5건·제외 11건 확인). 다른 보존 필드와
                    # 같은 패턴으로 막는다.
                    item['notion_page_id'] = existing.get('notion_page_id')
                    item['notion_dismissed'] = existing.get('notion_dismissed', 0)
                else:
                    # New Item
                    item['last_updated'] = item.get('scraped_at')
                    item['change_log'] = ""

                cursor.execute('''
                    INSERT INTO welfare_items (
                        site_name, title, link, date,
                        category, target, residence,
                        start_age_months, end_age_months, age_detail,
                        target_income, support_detail, eligibility_detail,
                        contact, event_date, deadline, matched_keyword, scraped_at,
                        last_updated, change_log,
                        ai_data, target_characteristics, life_cycle, content_hash, ai_call_at,
                        event_end_date, is_archived, page_modified_date, embedding, embedding_recipe,
                        ai_skip_reason, ai_last_attempt_at, notion_page_id, notion_dismissed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(link) DO UPDATE SET
                        site_name=excluded.site_name,
                        title=excluded.title,
                        date=excluded.date,
                        category=excluded.category,
                        target=excluded.target,
                        residence=excluded.residence,
                        start_age_months=excluded.start_age_months,
                        end_age_months=excluded.end_age_months,
                        age_detail=excluded.age_detail,
                        target_income=excluded.target_income,
                        support_detail=excluded.support_detail,
                        eligibility_detail=excluded.eligibility_detail,
                        contact=excluded.contact,
                        event_date=excluded.event_date,
                        deadline=excluded.deadline,
                        matched_keyword=excluded.matched_keyword,
                        scraped_at=excluded.scraped_at,
                        last_updated=excluded.last_updated,
                        change_log=excluded.change_log,
                        ai_data=excluded.ai_data,
                        target_characteristics=excluded.target_characteristics,
                        life_cycle=excluded.life_cycle,
                        content_hash=excluded.content_hash,
                        ai_call_at=excluded.ai_call_at,
                        event_end_date=excluded.event_end_date,
                        is_archived=excluded.is_archived,
                        page_modified_date=excluded.page_modified_date,
                        embedding=excluded.embedding,
                        embedding_recipe=excluded.embedding_recipe,
                        ai_skip_reason=excluded.ai_skip_reason,
                        ai_last_attempt_at=excluded.ai_last_attempt_at,
                        notion_page_id=excluded.notion_page_id,
                        notion_dismissed=excluded.notion_dismissed
                    WHERE NOT EXISTS (SELECT 1 FROM news_result_imports ni
                        WHERE ni.item_id=welfare_items.id AND ni.link=welfare_items.link
                        AND ni.outcome='created')
                    ''', (
                        item.get('site_name'), item.get('title'), item.get('link'), item.get('date'),
                        item.get('category'), item.get('target'), item.get('residence'),
                        item.get('start_age_months'), item.get('end_age_months'), item.get('age_detail'),
                        item.get('target_income'), item.get('support_detail'), item.get('eligibility_detail'),
                        item.get('contact'), item.get('event_date'), item.get('deadline'),
                        item.get('matched_keyword'), item.get('scraped_at') or datetime.now().isoformat(),
                        item.get('last_updated'), item.get('change_log'),
                        item.get('ai_data'), item.get('target_characteristics'), item.get('life_cycle'),
                        item.get('content_hash'), item.get('ai_call_at'),
                        item.get('event_end_date'), item.get('is_archived', 0), item.get('page_modified_date'),
                        item.get('embedding_blob'), item.get('embedding_recipe'),
                        item.get('ai_skip_reason'), item.get('ai_last_attempt_at'),
                        item.get('notion_page_id'), item.get('notion_dismissed', 0)
                    ))
                if cursor.rowcount == 0:
                    skipped += 1
                    if item.get('link'):
                        skipped_links.append(item['link'])
                    continue
                count += 1
                if item.get('link') and item['link'] not in saved_link_set:
                    saved_link_set.add(item['link'])
                    saved_links.append(item['link'])
            
            conn.commit()
            result = {
                "requested": len(items),
                "saved": len(saved_links),
                "processed": count,
                "skipped": skipped,
                "saved_links": saved_links,
                "skipped_links": skipped_links,
            }
            logger.info(
                f"Saved {result['saved']} items to DB"
                f" (processed {count}, skipped {skipped}).")
            return result
        
    except Exception as e:
        logger.error(f"DB Save Data Error: {e}")
        raise


def is_new(item: Dict[str, Any], history: List[str]) -> bool:
    """
    Checks if an item is new based on its link.
    History is a list of links (retrieved from DB).
    """
    return item.get('link') not in history


# ==================== AI 분석 백로그 (2026-08-06) ====================
# 하루 자체 상한(150건) 초과 등으로 AI 분석 없이 저장된 항목은, 링크가 이미 DB에 있다는
# 이유만으로 다음 수집에서 "이미 수집된 항목"으로 걸러져 영구히 재분석 대상에서 빠졌다
# (실측: 운영 DB 191건, 2026-08-06 확인). 사이트에 다시 나타나길 기다리지 않고 DB에서
# 직접 재분석 대상을 뽑아내기 위한 조회 함수.
from services.legacy_news_analysis import BACKLOG_CANDIDATE_SQL as _BACKLOG_WHERE, backlog_due


def count_ai_backlog() -> int:
    """재분석 대상(AI 미완료 & 영구실패 아님) 총 건수."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT ai_data, ai_skip_reason, ai_last_attempt_at FROM welfare_items WHERE {_BACKLOG_WHERE}")
            return sum(backlog_due(dict(row)) for row in cursor)
    except Exception as e:
        logger.error(f"Error counting AI backlog: {e}")
        raise


def count_ai_permanently_excluded() -> int:
    """(2026-08-06, GPT Codex 3차 리뷰) failed_permanent로 표시돼 백로그에서 빠진 항목 수.

    AI 오분류·일시적 로그인 리다이렉트 한 번만으로 실제 복지정보가 영구 제외될 위험을
    관찰하기 위한 지표 — 이 값이 늘어나면 오탐 여부를 점검해야 한다는 신호."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM welfare_items WHERE ai_skip_reason='failed_permanent'")
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"Error counting permanently excluded AI items: {e}")
        raise


def get_ai_backlog(limit: int) -> List[Dict[str, Any]]:
    """재분석 대상을 우선순위대로 뽑는다.

    한 번도 시도하지 않은 항목(ai_last_attempt_at IS NULL)을 먼저 처리하고, 그 다음은
    가장 오래전에 실패한 항목부터 처리한다. 이렇게 해야 계속 실패하는 항목이 매 실행마다
    큐 맨 앞을 점유해 다른 대기 항목을 밀어내는 편향을 막는다(GPT Codex 리뷰로 발견).
    """
    if limit <= 0:
        return []
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT * FROM welfare_items
                WHERE {_BACKLOG_WHERE}
                ORDER BY
                    CASE WHEN ai_last_attempt_at IS NULL THEN 0 ELSE 1 END,
                    ai_last_attempt_at ASC,
                    scraped_at ASC
            """)
            result = []
            for row in cursor:
                item = dict(row)
                if backlog_due(item):
                    result.append(item)
                    if len(result) >= limit:
                        break
            return result
    except Exception as e:
        logger.error(f"Error fetching AI backlog: {e}")
        raise


def search_items(query: str = None, category: str = None, status: str = None,
                 date_from: str = None, date_to: str = None, site_name: str = None,
                 limit: int = 50, offset: int = 0,
                 active_only: bool = False, include_archived: bool = False,
                 _as_of_date: str = None) -> Dict[str, Any]:
    """
    Advanced search with filters. Returns paginated results.
    date_from / date_to: 'YYYY-MM-DD' 형식 (scraped_at 기준)
    site_name: 수집 사이트 이름 일치 필터
    active_only: True이면 마감일이 지나지 않은 항목만 반환
    include_archived: True이면 아카이브된 항목도 포함
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()

            sql = "SELECT * FROM welfare_items WHERE 1=1"
            count_sql = "SELECT COUNT(*) as total FROM welfare_items WHERE 1=1"
            params = []

            # 아카이브 제외 (기본값: 아카이브 숨김)
            if not include_archived:
                sql += " AND (is_archived IS NULL OR is_archived = 0)"
                count_sql += " AND (is_archived IS NULL OR is_archived = 0)"

            if query:
                filter_clause = " AND id IN (SELECT rowid FROM welfare_items_fts WHERE welfare_items_fts MATCH ?)"
                sql += filter_clause
                count_sql += filter_clause
                # FTS 특수문자 제거: *, ?, [, ], (, ), ^, "', ' 가 FTS 파서 오류 유발
                safe_query = re.sub(r'[*?\[\]()\^"\']+', '', query)
                terms = [f'"{term}"*' for term in safe_query.split() if term.strip()]
                if not terms:
                    # 특수문자만으로 구성된 검색어 → FTS 스킵, LIKE fallback
                    sql = sql.replace(filter_clause, " AND (title LIKE ? OR support_detail LIKE ?)")
                    count_sql = count_sql.replace(filter_clause, " AND (title LIKE ? OR support_detail LIKE ?)")
                    like_param = f"%{query.strip()}%"
                    params.append(like_param)
                    params.append(like_param)
                else:
                    fts_query = " AND ".join(terms)
                    params.append(fts_query)

            if category:
                filter_clause = " AND category = ?"
                sql += filter_clause
                count_sql += filter_clause
                params.append(category)

            search_day = _as_of_date or search_dates.today_kst()
            if active_only:
                predicate, date_params = search_dates.deadline_filter(active=True, as_of_date=search_day)
                filter_clause = " AND " + predicate
                sql += filter_clause
                count_sql += filter_clause
                params.extend(date_params)

            if status:
                if status in ("active", "closed"):
                    predicate, date_params = search_dates.deadline_filter(
                        active=status == 'active', as_of_date=search_day)
                    filter_clause = " AND " + predicate
                    sql += filter_clause
                    count_sql += filter_clause
                    params.extend(date_params)

            if date_from:
                filter_clause = " AND DATE(scraped_at) >= ?"
                sql += filter_clause
                count_sql += filter_clause
                params.append(date_from)

            if date_to:
                filter_clause = " AND DATE(scraped_at) <= ?"
                sql += filter_clause
                count_sql += filter_clause
                params.append(date_to)

            if site_name:
                filter_clause = " AND site_name = ?"
                sql += filter_clause
                count_sql += filter_clause
                params.append(site_name)

            cursor.execute(count_sql, params)
            count_row = cursor.fetchone()
            total = count_row["total"] if count_row else 0

            sql += " ORDER BY scraped_at DESC LIMIT ? OFFSET ?"
            cursor.execute(sql, params + [limit, offset])
            rows = cursor.fetchall()

            # load_data()와 일관성 유지: ai_data JSON 문자열을 파싱하여 객체로 반환
            items = []
            for row in rows:
                item = _strip_blob(dict(row))
                item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                items.append(item)

            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset
            }
    except Exception as e:
        logger.error(f"DB Search Error: {e}")
        raise


def get_recent_items(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Returns the most recently scraped items for the activity feed.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, site_name, title, link, category, target, target_characteristics, matched_keyword, scraped_at, last_updated, change_log, ai_data FROM welfare_items ORDER BY last_updated DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
            items = []
            for row in rows:
                item = _strip_blob(dict(row))
                item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                items.append(item)
            return items
    except Exception as e:
        logger.error(f"DB Recent Items Error: {e}")
        raise


def get_items_by_links(links: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    주어진 링크 목록과 일치하는 항목을 DB에서 조회하여 {link: item} 딕셔너리로 반환.
    load_data() 전체 로드 대신 필요한 링크만 조회하는 최적화 함수.

    SQLite IN 절의 최대 파라미터 한계(999개)를 피하기 위해 500개씩 청크로 분할 처리.
    """
    if not links:
        return {}
    _CHUNK = 500  # SQLite 파라미터 한계 999에 여유를 둔 배치 크기
    result: Dict[str, Dict[str, Any]] = {}
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(0, len(links), _CHUNK):
                chunk = links[i:i + _CHUNK]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(
                    f"SELECT * FROM welfare_items WHERE link IN ({placeholders})",
                    chunk
                )
                for row in cursor.fetchall():
                    item = _strip_blob(dict(row))
                    item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                    result[item['link']] = item
        return result
    except Exception as e:
        logger.error(f"DB Get Items By Links Error: {e}")
        raise




def get_item_by_id(item_id: int) -> Optional[Dict[str, Any]]:
    """
    Returns a single welfare item by its ID with all fields.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM welfare_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
            if row:
                item = _strip_blob(dict(row))
                # 다른 함수들(load_data, search_items 등)과 동일하게 ai_data 키를 직접 교체
                item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                return item
            return None
    except Exception as e:
        logger.error(f"DB Get Item Error: {e}")
        raise

_EXPORT_COLUMNS = [
    ('site_name',             '수집 사이트'),
    ('title',                 '제목'),
    ('category',              '카테고리'),
    ('target',                '대상'),
    ('target_characteristics','대상 특성'),
    ('life_cycle',            '생애주기'),
    ('residence',             '거주지'),
    ('age_detail',            '연령 상세'),
    ('start_age_months',      '시작 월령(개월)'),
    ('end_age_months',        '종료 월령(개월)'),
    ('target_income',         '소득 수준'),
    ('support_detail',        '지원 내용'),
    ('eligibility_detail',    '자격 요건'),
    ('contact',               '문의처'),
    ('deadline',              '마감일'),
    ('event_date',            '행사 시작일'),
    ('event_end_date',        '행사 종료일'),
    ('link',                  '원문 링크'),
    ('matched_keyword',       '매칭 키워드'),
    ('scraped_at',            '수집일시'),
    ('last_updated',          '최종 업데이트'),
    ('change_log',            '변경 이력'),
    ('page_modified_date',    '페이지 수정일'),
    # ai_data에서 추출
    ('_ai_source_site_name',  '출처 기관'),
    ('_ai_item_type',         '항목 유형'),
    ('_ai_related_urls',      '관련 링크'),
]



def _flatten_for_export(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """내보내기용으로 ai_data를 펼치고, 한글 컬럼명으로 변환."""
    col_map = {db_col: label for db_col, label in _EXPORT_COLUMNS}
    rows = []
    for item in items:
        ai = item.get('ai_data') or {}
        if isinstance(ai, str):
            ai = _parse_ai_data(ai)

        row = {}
        for db_col, label in _EXPORT_COLUMNS:
            if db_col == '_ai_source_site_name':
                row[label] = ai.get('source_site_name', '')
            elif db_col == '_ai_item_type':
                row[label] = ai.get('item_type', '')
            elif db_col == '_ai_related_urls':
                urls = ai.get('related_urls', [])
                row[label] = ', '.join(urls) if isinstance(urls, list) else str(urls or '')
            else:
                row[label] = item.get(db_col, '')
        # 수집한 문자열이 Excel/CSV에서 수식으로 실행되지 않도록 텍스트로 보존.
        row = {key: ("'" + value if isinstance(value, str) and
                     value.lstrip().startswith(('=', '+', '-', '@')) else value)
               for key, value in row.items()}
        rows.append(row)
    return rows



def export_data(format: str = "csv", category: str = None, status: str = None,
                date_from: str = None, date_to: str = None, site_name: str = None) -> str:
    """
    Exports data to a file and returns the file path.
    필터가 하나라도 있으면 search_items()로 필터링, 없으면 전체 데이터 내보내기.
    """
    try:
        # 검색/홈 화면에서는 필요 없는 무거운 모듈이다. 내보내기를 실제로
        # 요청할 때만 로드하며, 실패는 기존 내보내기 오류 처리로 전달한다.
        import pandas as pd

        # 필터 유무와 관계없이 search_items()를 통해 limit 적용 (OOM 방지)
        # EXPORT_MAX_ROWS 환경변수로 조정 가능 (기본 50,000건)
        export_limit = int(os.getenv('EXPORT_MAX_ROWS', '50000'))
        result = search_items(
            category=category, status=status,
            date_from=date_from, date_to=date_to,
            site_name=site_name, limit=export_limit
        )
        items = result.get("items", [])

        export_rows = _flatten_for_export(items)
        # 컬럼 순서 유지
        col_order = [label for _, label in _EXPORT_COLUMNS]
        df = pd.DataFrame(export_rows, columns=col_order)

        from services.temp_files import create_temp_file, delete_temp_file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = '.xlsx' if format.lower() == 'excel' else '.csv'
        fd, file_path = create_temp_file(prefix=f'welfare_export_{timestamp}_', suffix=suffix)
        os.close(fd)

        try:
            if format.lower() == "excel":
                df.to_excel(file_path, index=False)
            else:
                df.to_csv(file_path, index=False, encoding="utf-8-sig")
        except BaseException:
            delete_temp_file(file_path)
            raise

        return file_path
    except Exception as e:
        logger.error(f"Data Export Error: {e}")
        return ""


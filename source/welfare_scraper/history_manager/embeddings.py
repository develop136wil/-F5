"""AI 캐시 조회, 임베딩(의미 검색/중복 제거) 관련 DB 접근."""
import re
from typing import List, Dict, Any, Optional
import database
from services.logger import setup_logger
from services import search_dates
from .common import _parse_ai_data

logger = setup_logger(__name__)


def _ensure_embedding_recipe_column() -> bool:
    """lifespan 없이 함수를 직접 부르는 테스트/도구에서도 소규모 마이그레이션을 보장한다."""
    with database.get_connection() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(welfare_items)')}
        if 'embedding_recipe' not in columns:
            try:
                conn.execute('ALTER TABLE welfare_items ADD COLUMN embedding_recipe TEXT')
                conn.commit()
            except Exception as exc:
                # 다른 스레드가 같은 컬럼을 먼저 추가한 경우만 허용한다.
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(welfare_items)')}
                if 'embedding_recipe' not in columns:
                    logger.debug("embedding_recipe 마이그레이션은 앱 초기화 때 재시도: %s", exc)
                    raise
        return True

def find_cached_ai_data_by_hash(content_hash: str) -> Optional[Dict[str, Any]]:
    """
    Finds and returns previously parsed AI data if an exact text match exists.
    """
    if not content_hash:
        return None
        
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ai_data 
                FROM welfare_items 
                WHERE content_hash = ? AND ai_data IS NOT NULL AND ai_data != '{}'
                ORDER BY scraped_at DESC LIMIT 1
            ''', (content_hash,))
            
            row = cursor.fetchone()
            if row and row['ai_data']:
                # _parse_ai_data 헬퍼로 일관성 유지 (JSONDecodeError 안전 처리 포함)
                parsed = _parse_ai_data(row['ai_data'])
                return parsed if parsed else None
    except Exception as e:
        logger.warning(f"Error looking up AI cache for hash {content_hash}: {e}")
        raise
        
    return None

# ── 임베딩 (의미 검색 / 중복 제거) ────────────────────────────────────────────

def save_embedding(link: str, blob: Optional[bytes], recipe: Optional[str] = None) -> bool:
    """단일 항목의 임베딩 BLOB을 저장하고 실제 갱신 여부를 반환한다."""
    if not link or blob is None:
        return False
    if not _ensure_embedding_recipe_column():
        return False
    with database.get_connection() as conn:
        cursor = conn.execute(
            "UPDATE welfare_items SET embedding = ?, embedding_recipe = ? WHERE link = ?",
            (blob, recipe, link))
        conn.commit()
        return cursor.rowcount > 0



def get_items_missing_embedding(limit: int = 200, *, raise_on_error: bool = True,
                                after_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """검색 벡터가 없거나 현재 레시피와 다른 항목을 유한 페이지로 반환한다.

    after_id는 백필 전용 키셋 커서다. 성공 항목이 결과에서 빠져도 OFFSET처럼
    다음 항목을 건너뛰지 않으며, 보류 항목 뒤쪽도 다음 페이지에서 처리할 수 있다.
    """
    try:
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ValueError('Invalid embedding scan limit')
        if after_id is not None and (type(after_id) is not int or not 0 <= after_id <= 9223372036854775807):
            raise ValueError('Invalid embedding scan cursor')
        from services import embedding_service
        has_recipe = _ensure_embedding_recipe_column()
        with database.get_connection() as conn:
            cursor = conn.cursor()
            recipe_clause = "(embedding IS NULL OR COALESCE(embedding_recipe, '') != ?)" if has_recipe else "1=1"
            params = [embedding_service.SEARCH_EMBEDDING_RECIPE] if has_recipe else []
            cursor_clause = 'AND id > ?' if after_id is not None else ''
            order_clause = 'id ASC' if after_id is not None else 'scraped_at DESC, id DESC'
            if after_id is not None:
                params.append(after_id)
            params.append(limit)
            cursor.execute(
                f"""SELECT id, link, title, support_detail, category, target, residence,
                          target_characteristics, life_cycle, age_detail, target_income,
                          eligibility_detail
                   FROM welfare_items
                   WHERE {recipe_clause}
                     AND link IS NOT NULL AND link != ''
                     {cursor_clause}
                   ORDER BY {order_clause} LIMIT ?""",
                params,
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"get_items_missing_embedding error: {e}")
        if raise_on_error:
            raise
        return []



def count_embeddings(*, raise_on_error: bool = True) -> Dict[str, int]:
    """임베딩 보유/미보유 항목 수 (백필 진행률 표시용)."""
    try:
        from services import embedding_service
        has_recipe = _ensure_embedding_recipe_column()
        with database.get_connection() as conn:
            cursor = conn.cursor()
            if has_recipe:
                cursor.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN embedding IS NOT NULL AND embedding_recipe = ? THEN 1 ELSE 0 END) AS embedded, "
                    "SUM(CASE WHEN embedding IS NOT NULL AND COALESCE(embedding_recipe, '') != ? THEN 1 ELSE 0 END) AS legacy "
                    "FROM welfare_items",
                    (embedding_service.SEARCH_EMBEDDING_RECIPE, embedding_service.SEARCH_EMBEDDING_RECIPE),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) AS total, 0 AS embedded, "
                    "SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS legacy FROM welfare_items")
            row = cursor.fetchone()
            total = row['total'] or 0
            embedded = row['embedded'] or 0
            return {"total": total, "embedded": embedded, "missing": total - embedded,
                    "legacy": row['legacy'] or 0}
    except Exception as e:
        logger.error(f"count_embeddings error: {e}")
        if raise_on_error:
            raise
        return {"total": 0, "embedded": 0, "missing": 0, "legacy": 0}



def get_embedding_index(limit: int = 20000) -> List[Dict[str, Any]]:
    """ai_data와 임베딩을 모두 가진 항목을 반환(스크래핑 중 의미 중복 검사용).

    스크래핑 1회당 한 번만 로드해 메모리에서 매칭하면 항목별 DB 스캔을 피할 수 있다.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT link, ai_data, embedding FROM welfare_items
                   WHERE embedding IS NOT NULL AND ai_data IS NOT NULL AND ai_data != '{}'
                   ORDER BY scraped_at DESC LIMIT ?""",
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"get_embedding_index error: {e}")
        raise



def semantic_search(query_vec, limit: int = 20, min_score: float = 0.55,
                    include_archived: bool = False, active_only: bool = False,
                    category: str = None, _as_of_date: str = None) -> Dict[str, Any]:
    """질의 임베딩 벡터로 의미 기반 검색. 코사인 유사도 상위 limit개 반환.

    query_vec: list[float] 또는 np.ndarray (호출자가 RETRIEVAL_QUERY로 임베딩).
    반환: {"items": [...각 항목 dict + 'score'...], "total": N}
    """
    if query_vec is None:
        return {"items": [], "total": 0}
    try:
        import numpy as np
        from services import embedding_service
        if not _ensure_embedding_recipe_column():
            return {"items": [], "total": 0}

        q = np.asarray(query_vec, dtype='float32')
        sql = ("SELECT id, site_name, title, link, date, category, target, residence, "
               "deadline, event_date, event_end_date, scraped_at, support_detail, embedding "
               "FROM welfare_items WHERE embedding IS NOT NULL AND embedding_recipe = ?")
        params = [embedding_service.SEARCH_EMBEDDING_RECIPE]
        if not include_archived:
            sql += " AND (is_archived IS NULL OR is_archived = 0)"
        if active_only:
            predicate, date_params = search_dates.deadline_filter(active=True, as_of_date=_as_of_date)
            sql += " AND " + predicate
            params.extend(date_params)
        if category:
            sql += " AND category = ?"
            params.append(category)
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        candidates = []
        for r in rows:
            vec = embedding_service.from_blob(r['embedding'])
            if vec is None:
                continue
            payload = {k: r[k] for k in r.keys() if k != 'embedding'}
            candidates.append((payload, vec))

        ranked = embedding_service.rank_by_similarity(q, candidates, limit=limit, min_score=min_score)
        items = [{**payload, "score": round(score, 4)} for payload, score in ranked]
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.error(f"semantic_search error: {e}")
        raise


def hybrid_search(query_text: str, query_vec=None, limit: int = 20,
                  min_score: float = 0.55, include_archived: bool = False,
                  active_only: bool = False, category: str = None) -> Dict[str, Any]:
    """정확한 단어 검색과 의미 검색을 RRF로 결합한다.

    키워드 일치는 임베딩 한도와 무관하게 유지되고, 의미 검색은 표현이 다른 관련 자료를
    보완한다. 카테고리는 후보를 뽑기 전에 DB에서 적용한다.
    """
    from .items import search_items

    candidate_limit = max(100, limit * 5)
    search_day = search_dates.today_kst()
    lexical = search_items(
        query=query_text, category=category, limit=candidate_limit, offset=0,
        active_only=active_only, include_archived=include_archived, _as_of_date=search_day,
    ).get('items', [])

    query_terms = [term.casefold() for term in re.findall(r'[\w가-힣]+', query_text or '') if term]
    query_folded = ' '.join(query_terms)

    # FTS 기본 검색은 여러 단어를 AND로 묶는다. 자연어 문장이 길면 한 단어만 빠져도
    # 정확 단어 후보가 0건이 되므로, 결과가 적을 때 의미 있는 개별 단어 검색으로 보완한다.
    # '지원/사업/안내' 같은 일반어만으로 후보가 범람하지 않게 제외한다.
    # 단독으로는 범위가 지나치게 넓은 말은 부분 일치 후보 확장에 쓰지 않는다.
    # 특히 '한부모 가정' 같은 질의에서 '가정'만 일치하는 일반 양육 행사가
    # 핵심 대상어인 '한부모' 결과보다 앞서는 현상을 막는다.
    generic_terms = {
        '지원', '사업', '서비스', '정보', '안내', '신청', '정책', '복지', '가정',
    }
    exact_lexical_keys = {item.get('id') or item.get('link') for item in lexical}
    lexical_by_key = {item.get('id') or item.get('link'): item for item in lexical}
    meaningful_terms = [term for term in query_terms if len(term) >= 2 and term not in generic_terms]
    if len(lexical) < limit and len(meaningful_terms) > 1:
        for term in meaningful_terms[:5]:
            partial = search_items(
                query=term, category=category, limit=candidate_limit, offset=0,
                active_only=active_only, include_archived=include_archived, _as_of_date=search_day,
            ).get('items', [])
            for item in partial:
                key = item.get('id') or item.get('link')
                lexical_by_key.setdefault(key, item)
        lexical = list(lexical_by_key.values())

    def lexical_quality(item):
        title = str(item.get('title') or '').casefold()
        detail = ' '.join(str(item.get(key) or '') for key in (
            'support_detail', 'target', 'target_characteristics', 'life_cycle',
            'residence', 'eligibility_detail',
        )).casefold()
        title_hits = sum(term in title for term in query_terms)
        detail_hits = sum(term in detail for term in query_terms)
        phrase = bool(query_folded and query_folded in ' '.join(title.split()))
        return (phrase, title_hits, detail_hits, str(item.get('scraped_at') or ''))

    lexical.sort(key=lexical_quality, reverse=True)
    semantic = []
    if query_vec is not None:
        semantic = semantic_search(
            query_vec, limit=candidate_limit, min_score=min_score,
            include_archived=include_archived, active_only=active_only,
            category=category, _as_of_date=search_day,
        ).get('items', [])

    fused = {}
    rrf_k = 60.0
    for rank, item in enumerate(lexical, 1):
        key = item.get('id') or item.get('link')
        payload = dict(item)
        payload['keyword_match'] = 'all_terms' if key in exact_lexical_keys else 'partial_terms'
        fused[key] = {"item": payload, "rank_score": 1.35 / (rrf_k + rank),
                      "sources": {'keyword'}}
    for rank, item in enumerate(semantic, 1):
        key = item.get('id') or item.get('link')
        row = fused.setdefault(key, {"item": dict(item), "rank_score": 0.0,
                                     "sources": set()})
        row['rank_score'] += 1.0 / (rrf_k + rank)
        row['sources'].add('semantic')
        row['item']['semantic_score'] = item.get('score')

    total_candidates = len(fused)
    ordered = sorted(fused.values(), key=lambda row: row['rank_score'], reverse=True)[:limit]
    items = []
    for row in ordered:
        item = row['item']
        item['score'] = round(row['rank_score'], 6)
        item['match_sources'] = sorted(row['sources'])
        items.append(item)
    return {"items": items, "total": total_candidates, "returned": len(items),
            "semantic_used": query_vec is not None, "ranking": "hybrid-rrf-v1"}



def get_daily_ai_count() -> int:
    """오늘 AI 처리 파이프라인을 시도한 항목 수 반환(캐시 히트 제외, 일반수집 분류 대상 기준).

    (2026-08-06) 예전엔 ai_call_at(성공 시에만 찍힘) 기준이라 429 등으로 실패한 호출은
    자체 하루 상한(150건) 집계에서 빠졌다 — 실패해도 Google 쪽 실제 호출은 소비되므로,
    실패가 반복되는 날엔 "카운터는 안전한데 실제 소비량은 위험"한 괴리가 생겼다
    (GPT Codex 리뷰로 발견). ai_last_attempt_at(성공·실패 모두 기록)으로 기준을 바꿨다.

    (2026-08-06, 2차 리뷰) 단, 이 값은 "실제 Gemini 호출 수"와 여전히 다르다 —
    ai_last_attempt_at은 함수 진입 직후 항상 찍히므로 로그인 페이지 감지·fetch 자체
    실패처럼 Gemini를 아예 안 부른 경우까지 포함한다(보수적으로 안전한 방향의 오차).
    또한 내부 재시도(최대 3회)·OCR·Vision·정책추출·필드비교임베딩 등 이 카운터에
    안 잡히는 별도 Gemini 호출 경로가 있다. 이 값은 과거 처리량을 보여주는 통계일 뿐,
    실제 Google 사용량이나 잔여량이 아니며 더 이상 일일 차단에 사용하지 않는다."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM welfare_items
                WHERE DATE(ai_last_attempt_at) = DATE('now', 'localtime')
            """)
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"Error fetching daily AI count: {e}")
        raise


def get_similar_items(item_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """
    FTS5 BM25 랭킹을 이용해 주어진 항목과 유사한 항목을 반환.
    - 대상 항목의 제목 키워드를 OR 조합으로 FTS 검색
    - rank(BM25, 낮을수록 관련성 높음) 기준 정렬
    - 대상 항목 자신은 제외
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT title FROM welfare_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
            if not row:
                return []

            title = row['title'] or ''
            # FTS 특수문자 제거 후 개별 단어를 OR 조합 쿼리로 구성
            safe_title = re.sub(r'[*?\[\]()\^"\']+', '', title)
            terms = [f'"{t}"' for t in safe_title.split() if t.strip() and len(t) > 1]
            if not terms:
                return []
            # 최대 10개 키워드만 사용 (긴 제목 대응)
            fts_query = ' OR '.join(terms[:10])

            cursor.execute('''
                SELECT wi.id, wi.title, wi.link, wi.category, wi.site_name,
                       wi.target, wi.deadline, wi.scraped_at, fts.rank
                FROM welfare_items_fts fts
                JOIN welfare_items wi ON fts.rowid = wi.id
                WHERE welfare_items_fts MATCH ?
                  AND wi.id != ?
                ORDER BY fts.rank
                LIMIT ?
            ''', (fts_query, item_id, limit))

            return [
                {k: v for k, v in dict(r).items() if k != 'rank'}
                for r in cursor.fetchall()
            ]
    except Exception as e:
        logger.error(f"Similar Items Error: {e}")
        raise



"""Default news condition, versioned with the same inputs as custom runs.

Never changes legacy settings, starts collection, or relaxes quality gates.
"""
from dataclasses import replace
from services.news_auto_sources import automatic_sources
from services import news_profile_store as store

PROFILE_ID = 'default-news'


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def legacy_collection_available(conn):
    """Positive evidence of prior legacy use, never registration/key presence.

    This is a read-only UI compatibility hint, not permission to mutate a profile.
    Missing history is deliberately not interpreted as consent to legacy defaults.
    """
    if _has_table(conn, 'news_profiles') and conn.execute(
            "SELECT 1 FROM news_profiles WHERE profile_id='legacy-infant' AND protected=1 LIMIT 1").fetchone():
        return True
    # Only the legacy scraper writes this table; site registration/tests do not.
    if _has_table(conn, 'scrape_runs') and conn.execute('SELECT 1 FROM scrape_runs LIMIT 1').fetchone():
        return True
    if _has_table(conn, 'welfare_items'):
        adopted = ("AND NOT EXISTS (SELECT 1 FROM news_result_imports i WHERE i.item_id=w.id)"
                   if _has_table(conn, 'news_result_imports') else '')
        return bool(conn.execute("""SELECT 1 FROM welfare_items w WHERE
            json_extract(CASE WHEN json_valid(ai_data) THEN ai_data ELSE '{}' END,'$.item_type')
            IN ('program','event') """ + adopted + ' LIMIT 1').fetchone())
    return False


def initial_settings(conn, values, *, legacy=False, default_keywords=(), default_excluded=()):
    """Whitelisted, unpersisted editor seed; never include keys or adopt defaults."""
    result = {}
    region = values.get('TARGET_REGION')
    if isinstance(region, str) and region.strip():
        result['region'] = region
    elif legacy:
        result['region'] = '도봉'
    age = values.get('TARGET_AGE_MAX_MONTHS', 108 if legacy else None)
    if age is not None and str(age).isdigit() and 0 <= int(age) <= 1800:
        result.update(min_age_months=0, max_age_months=int(age))
    for source, field in (('CUSTOM_AI_PROMPT', 'ai_prompt'), ('RELEVANCE_PROFILE', 'semantic_text')):
        if isinstance(values.get(source), str):
            result[field] = values[source]
    if 'RELEVANCE_FILTER_ENABLED' in values or legacy:
        result['relevance_enabled'] = str(values.get('RELEVANCE_FILTER_ENABLED', '1')) != '0'
    # Merely viewing the old keyword API seeds these tables. Without actual
    # legacy history there is no provenance proving that these are user choices.
    if legacy:
        for table, fallback in (('keywords', default_keywords), ('excluded_keywords', default_excluded)):
            words = [r[0] for r in conn.execute(f'SELECT keyword FROM {table}')] if _has_table(conn, table) else []
            result[table] = words or list(fallback)
    return result


def read(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_profiles'").fetchone():
        return None
    try:
        profile = store.get_profile(conn, PROFILE_ID)
    except KeyError:
        return None
    if conn.execute('SELECT archived FROM news_profiles WHERE profile_id=?', (PROFILE_ID,)).fetchone()[0]:
        raise ValueError('기본 수집 조건이 보관 처리되어 있습니다. 설정을 확인해주세요.')
    return profile


def save(conn, profile, expected_revision):
    with store._atomic(conn):
        store.ensure_schema(conn)
        current = read(conn)
        if expected_revision != (current.revision if current else 0):
            raise store.ProfileConflict('수집 조건이 바뀌었습니다. 다시 불러온 뒤 저장해주세요.')
        if profile.profile_id != PROFILE_ID:
            raise ValueError('기본 새 소식 조건의 식별자를 확인해주세요.')
        profile = automatic_sources(profile)
        return (store.save_revision(conn, profile, expected_revision=expected_revision)
                if current else store.create_profile(conn, profile))


def synchronize_sites(conn):
    """Default means all registered sites; preserve each previous run's snapshot."""
    with store._atomic(conn):
        profile = read(conn)
        if profile is None:
            return None
        ids = tuple(str(row[0]) for row in conn.execute('SELECT id FROM sites ORDER BY id'))
        updated = automatic_sources(replace(profile, site_ids=ids))
        if updated == profile:
            return profile
        return store.save_revision(conn, replace(updated, revision=profile.revision + 1),
                                   expected_revision=profile.revision)

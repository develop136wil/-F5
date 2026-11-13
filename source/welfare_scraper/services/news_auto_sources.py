"""Keep new news collections within the user's registered site selection.

Older source revisions and collected results remain readable, but an old run
that included automatic Bokjiro collection must not silently resume it.
"""
from dataclasses import replace


SOURCE_SCOPE_CHANGED_REASON = 'news_source_scope_changed'
RETIRED_NEWS_BOKJIRO_MESSAGE = (
    '새 소식의 복지로 목록 조회·일괄 수집 기능은 종료되었습니다. '
    '새 소식은 등록한 기관 사이트에서만 찾습니다. '
    '수집 사이트 등록·관리에서 기관 게시판 주소를 등록해주세요.'
)
SOURCE_SCOPE_CHANGED_MESSAGE = (
    '이전 수집에 복지로 자동 수집이 포함되어 있어 이어서 진행할 수 없습니다. '
    '저장한 기록은 보존됩니다. 새 소식 찾기로 등록한 사이트만 다시 수집해주세요.'
)


def retired_source_scope(profile_snapshot):
    return 'bokjiro' in profile_snapshot.get('sources', ())


def automatic_sources(profile):
    """Normalize current custom conditions; target labels never add sources."""
    if profile.general_mode != 'custom_v1':
        return profile
    return replace(profile, sources=('sites',), bokjiro_mode='unsupported')


def upgrade_saved_sources(conn):
    """Append new versions; historical profiles and run snapshots stay immutable."""
    from services import news_profile_store as store
    with store._atomic(conn):
        rows = conn.execute('SELECT profile_id FROM news_profiles WHERE protected=0 AND archived=0').fetchall()
        for row in rows:
            profile = store.get_profile(conn, row[0])
            if profile.general_mode != 'custom_v1':
                continue
            if profile.profile_id == 'default-news':
                ids = tuple(str(item[0]) for item in conn.execute('SELECT id FROM sites ORDER BY id'))
                updated = automatic_sources(replace(profile, site_ids=ids))
            else:
                updated = automatic_sources(profile)
            if updated != profile:
                store.save_revision(conn, replace(updated, revision=profile.revision + 1),
                                    expected_revision=profile.revision)

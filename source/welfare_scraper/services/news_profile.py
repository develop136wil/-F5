"""Immutable run configuration foundation; not yet wired into collection APIs.

No environment, database, or network reads. Callers supply a coherent snapshot.
Policy synchronization must not consume this news-only configuration.
"""
from dataclasses import dataclass, asdict
import hashlib
import json


@dataclass(frozen=True)
class NewsProfile:
    profile_id: str
    revision: int
    name: str
    region: str
    min_age_months: int | None = None
    max_age_months: int | None = None
    life_cycles: tuple[str, ...] = ()
    characteristics: tuple[str, ...] = ()
    interest_topics: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()
    site_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ('sites',)
    ai_prompt: str = ''
    semantic_text: str = ''
    relevance_enabled: bool = True
    general_mode: str = 'custom_v1'
    bokjiro_mode: str = 'unsupported'
    publication_period: str = '30'
    publication_start_date: str = ''
    publication_end_date: str = ''

    def __post_init__(self):
        from services.news_publication import validate_settings
        validate_settings(self.publication_period, self.publication_start_date, self.publication_end_date)
        for field in ('profile_id', 'name', 'region'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{field} must be a nonempty string')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('revision must be a positive integer')
        for field in ('min_age_months', 'max_age_months'):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or not 0 <= value <= 1800):
                raise ValueError(f'{field} must be null or integer months from 0 to 1800')
        if (self.min_age_months is not None and self.max_age_months is not None
                and self.min_age_months > self.max_age_months):
            raise ValueError('minimum age exceeds maximum age')
        for field in ('life_cycles', 'characteristics', 'interest_topics', 'keywords', 'excluded_keywords', 'site_ids', 'sources'):
            values = getattr(self, field)
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError(f'{field} must be a list of nonempty strings')
            # Copy mutable input without changing spelling/order used by legacy rules.
            object.__setattr__(self, field, tuple(values))
        if not self.sources or set(self.sources) - {'sites', 'bokjiro'}:
            raise ValueError('sources must select sites and/or bokjiro')
        if len(set(self.sources)) != len(self.sources):
            raise ValueError('duplicate source')
        if type(self.relevance_enabled) is not bool:
            raise ValueError('relevance_enabled must be boolean')
        if not isinstance(self.ai_prompt, str) or not isinstance(self.semantic_text, str):
            raise ValueError('analysis instructions must be strings')
        if self.general_mode not in {'legacy_infant_v1', 'custom_v1'}:
            raise ValueError('unknown general mode')
        if self.bokjiro_mode not in {'legacy_infant_v1', 'official_tags_v1', 'unsupported'}:
            raise ValueError('unknown Bokjiro mode')
        if self.bokjiro_mode == 'legacy_infant_v1' and self.general_mode != 'legacy_infant_v1':
            raise ValueError('custom profile cannot silently use infant Bokjiro rules')
        if self.bokjiro_mode == 'official_tags_v1' and self.general_mode != 'custom_v1':
            raise ValueError('official Bokjiro rules require a custom profile')
        if self.bokjiro_mode == 'official_tags_v1' and 'bokjiro' not in self.sources:
            raise ValueError('official Bokjiro mode requires the Bokjiro source')

    def snapshot(self) -> dict:
        """Detached JSON value for run persistence; no credential fields are collected.

        Free-text instructions may still contain sensitive user input: do not log
        or publish snapshots by default.
        """
        return json.loads(json.dumps({'schema_version': 2, **asdict(self)}, ensure_ascii=False))

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def require_bokjiro_support(self):
        """Validate an explicit Bokjiro translation; never fall back to infant rules.

        Custom labels may coexist for registered-site analysis. Only Bokjiro's
        official labels are returned for its collector, and at least one such
        label is required so a custom-only label cannot become an unbounded query.
        """
        if 'bokjiro' not in self.sources:
            return None
        if self.bokjiro_mode == 'unsupported':
            raise ValueError('이 프로필의 복지로 대상 조건은 아직 지원되지 않습니다.')
        if self.bokjiro_mode == 'legacy_infant_v1':
            return {'mode': 'legacy_infant_v1'}

        from services.news_profile_taxonomy import (
            LIFE_CYCLES, HOUSEHOLD_SITUATIONS, INTEREST_TOPICS,
            canonical_life_cycle, canonical_household_situation,
        )
        life_cycles = tuple(dict.fromkeys(
            value for value in map(canonical_life_cycle, self.life_cycles)
            if value in LIFE_CYCLES
        ))
        characteristics = tuple(dict.fromkeys(
            value for value in map(canonical_household_situation, self.characteristics)
            if value in HOUSEHOLD_SITUATIONS
        ))
        interest_topics = tuple(dict.fromkeys(
            value for value in self.interest_topics if value in INTEREST_TOPICS
        ))
        if not any((life_cycles, characteristics, interest_topics)):
            raise ValueError('복지로 수집에는 공식 생애주기·가구상황·관심주제를 하나 이상 선택해주세요.')
        return {
            'mode': 'official_tags_v1',
            'life_cycles': life_cycles,
            'characteristics': characteristics,
            'interest_topics': interest_topics,
        }


def capture_legacy_profile(*, region, max_age_months, keywords, excluded_keywords,
                           site_ids, ai_prompt, semantic_text, relevance_enabled,
                           include_bokjiro=False) -> NewsProfile:
    """Capture existing effective settings exactly, not a replacement default template.

    Bokjiro's historical region/infant rules remain identified by its legacy mode;
    they must not be reinterpreted as the general region/age range.
    """
    if type(max_age_months) is not int or not 0 <= max_age_months <= 240:
        raise ValueError('legacy maximum age must be 0..240 months')
    if type(include_bokjiro) is not bool:
        raise ValueError('include_bokjiro must be boolean')
    return NewsProfile(
        profile_id='legacy-infant', revision=1, name='기존 영유아·아동',
        region=region, min_age_months=0, max_age_months=max_age_months,
        keywords=keywords, excluded_keywords=excluded_keywords, site_ids=site_ids,
        sources=('sites', 'bokjiro') if include_bokjiro else ('sites',),
        ai_prompt=ai_prompt, semantic_text=semantic_text,
        relevance_enabled=relevance_enabled, general_mode='legacy_infant_v1',
        bokjiro_mode='legacy_infant_v1',
    )

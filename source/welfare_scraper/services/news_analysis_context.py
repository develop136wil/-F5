"""Explicit analysis inputs for opt-in news collection integration.

These helpers do not write results or launch jobs. Custom domain matching and
profile-aware caches/backlogs must be implemented before enabling custom runs.
"""
from dataclasses import dataclass
from services.news_profile import NewsProfile


@dataclass(frozen=True)
class NewsAnalysisContext:
    profile: NewsProfile

    @classmethod
    def from_run(cls, conn, run_id):
        from services.news_profile_store import get_run_snapshot
        payload = get_run_snapshot(conn, run_id)['profile']
        payload.pop('schema_version')
        return cls(NewsProfile(**payload))

    def route_title(self, title):
        """Excluded / keyword match / semantic candidate; no global keyword reads."""
        import content_filter
        if content_filter.is_excluded(title, keywords=self.profile.excluded_keywords):
            return 'excluded', ()
        # Bokjiro's public interest topics are useful search clues on registered
        # institution sites too. Keep the user's own words and add topics without
        # mutating either saved tuple.
        search_words = tuple(dict.fromkeys((*self.profile.keywords, *self.profile.interest_topics,
                                           *self.target_clues)))
        matched = content_filter.get_matched_keywords(title, keywords=search_words)
        return ('matched', tuple(matched)) if matched else ('semantic_candidate', ())

    @property
    def target_clues(self):
        if self.profile.general_mode != 'custom_v1':
            return ()
        return tuple(dict.fromkeys(value for value in (
            *self.profile.life_cycles, *self.profile.characteristics)
            if value not in ('일반', '전생애', '기타')))

    def body_target_hint(self, content):
        return tuple(dict.fromkeys(clue for block in self._body_target_blocks(content) for clue in block))

    def _body_target_blocks(self, content):
        """Extract bounded explicit target clues; never assert eligibility.

        Require an explicit target heading, not a mention in navigation, and
        leave absent/negative/ambiguous evidence unresolved.
        """
        import re
        if not isinstance(content, str) or not self.target_clues:
            return ()
        found = []
        lines = content[:12000].splitlines()
        # Never interpret a sentence cut off before its exclusion/qualifier.
        if len(content) > 12000 and not content[:12000].endswith(('\n', '\r')):
            lines = lines[:-1]
        heading = r'^\s*(?:지원\s*대상|신청\s*대상|참여\s*대상|모집\s*대상)\s*[:：]\s*(.*)$'
        for index, line in enumerate(lines):
            match = re.match(heading, line)
            if not match:
                continue
            evidence = match.group(1)
            if not evidence and index + 1 < len(lines):
                # Only the immediately adjacent line; never scan past a blank
                # line or into another labelled section.
                evidence = lines[index + 1].strip()
                if ':' in evidence or '：' in evidence:
                    continue
            if any(word in evidence for word in ('제외', '불가', '아닌', '아님', '아닙',
                                                 '해당하지', '대상이 아니', '대상 아니', '미해당')):
                continue
            # Alternative audiences do not establish a combined target.
            if any(word in evidence for word in ('또는', '혹은', '이거나')):
                continue
            block = []
            for clue in self.target_clues:
                if re.search(r'(?<![가-힣A-Za-z0-9])' + re.escape(clue)
                             + r'(?=$|[\s,·/()\[\]:：]|은|는|을|를|과|와)', evidence):
                    block.append(clue)
            if block:
                found.append(tuple(dict.fromkeys(block)))
        return tuple(found)

    def route_candidate(self, title, content):
        """Prefer explicit target headings before spending on title similarity.

        This only selects candidates for downstream AI analysis. Region/age,
        exclusions and eligibility still require the existing final target gate.
        Title exclusions always win; ambiguous bodies stay semantic candidates.
        """
        route, clues = self.route_title(title)
        if route != 'semantic_candidate' or self.profile.general_mode != 'custom_v1':
            return route, clues
        blocks = self._body_target_blocks(content)
        groups = [tuple(x for x in group if x not in ('일반', '전생애', '기타'))
                  for group in (self.profile.life_cycles, self.profile.characteristics)]
        groups = [group for group in groups if group]
        for evidence in blocks:
            if groups and all(any(x in evidence for x in group) for group in groups):
                return 'matched', evidence
        return route, clues

    @property
    def semantic_text(self):
        parts = [self.profile.semantic_text]
        if self.target_clues:
            parts.append('찾을 대상: ' + ', '.join(self.target_clues))
        if self.profile.interest_topics:
            parts.append('찾을 지원 분야: ' + ', '.join(self.profile.interest_topics))
        return '\n'.join(parts).strip()

    async def classify(self, client, *, title, link, site_name='', content=None):
        import ai_classifier
        self._require_legacy()
        return await ai_classifier.classify_post_async(
            client, title, link, site_name, content, custom_prompt=self.profile.ai_prompt)

    def post_filter(self, item):
        from services import relevance_filter
        self._require_legacy()
        # Copy: a preview or another profile must not overwrite the original
        # region_match / age_match flags on a shared candidate dictionary.
        result = dict(item)
        passed, reason = relevance_filter.passes_post_ai(result, {
            'enabled': self.profile.relevance_enabled,
            'target_region': self.profile.region,
            'max_months': self.profile.max_age_months,
        })
        return result, passed, reason

    def _require_legacy(self):
        if (self.profile.general_mode != 'legacy_infant_v1'
                or self.profile.min_age_months != 0
                or type(self.profile.max_age_months) is not int
                or not 0 <= self.profile.max_age_months <= 240
                or self.profile.life_cycles or self.profile.characteristics):
            raise ValueError('커스텀 대상 분석은 아직 실행 경로에 연결되지 않았습니다.')

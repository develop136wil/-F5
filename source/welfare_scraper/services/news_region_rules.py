"""Offline spelling normalization only; no geographic hierarchy inference."""

_PROVINCES = (
    ('서울특별시', '서울시', '서울'), ('부산광역시', '부산시', '부산'),
    ('대구광역시', '대구시', '대구'), ('인천광역시', '인천시', '인천'),
    ('광주광역시', '광주'), ('대전광역시', '대전시', '대전'),
    ('울산광역시', '울산시', '울산'), ('세종특별자치시', '세종시', '세종'),
    ('경기도', '경기'), ('강원특별자치도', '강원도', '강원'),
    ('충청북도', '충북'), ('충청남도', '충남'),
    ('전북특별자치도', '전라북도', '전북'), ('전라남도', '전남'),
    ('경상북도', '경북'), ('경상남도', '경남'), ('제주특별자치도', '제주도', '제주'),
)
_ALIASES = {alias: group[0] for group in _PROVINCES for alias in group}

# Reviewed aliases only. Never infer arbitrary district parents by dropping a
# province: 중구/서구/동구/남구/북구 and 광주시 are intentionally NOT here.
# Source verified 2026-09-08: https://www.dobong.go.kr/Contents.asp?code=10009546
_VERIFIED_DISTRICT_ALIASES = {'서울특별시 도봉구': ('도봉구',)}


def _registered_residence_matches(region_pattern, quote):
    """Recognize a bounded duration + registration predicate, not a place.

    This is a second, conservative spelling path. It does not supply a region,
    infer its parent, rewrite evidence, or decide that the entire source is
    affirmative. Callers still own source/section/negative-context checks.
    """
    import re
    if len(quote) > 2000:
        return False
    compact = re.sub(r'\s+', '', quote)
    # A duration does not turn a venue, example, proxy's address or a negative
    # condition into the beneficiary's residence. Ambiguity stays unknown.
    if re.search(
            r'제외|불가|불필요|아니|아님|아닙|않|없|비거주|미거주|무관|관계없|'
            r'미지원|미지급|미수급|금지|안됨|안됩니다|전출|이사|타지역|다른지역|'
            r'예외|우선|우대|통계|현황|조사|예시|예제|소재|주소|행사|장소|기관|'
            r'보호자|(?<!한)부모|부친|모친|대리인|제공인력|종사자', compact):
        return False
    # Do not bridge a physical line or insert arbitrary words between the
    # named locality and residence predicate. 1..999 is a grammar bound only,
    # never a calculated eligibility duration.
    space = r'[ \t\u00a0]*'
    pattern = (region_pattern + space + '에' + space
               + r'[1-9][0-9]{0,2}' + space + r'(?:년|개월)' + space + '이상' + space
               + r'주민등록(?:상' + space + '|을' + space + '두고' + space + ')'
               + r'거주(?:한|하는|하시는)(?=$|[\s,.;:!?。])')
    return re.search(pattern, quote) is not None


def residence_quote_matches(value, quote):
    """Match explicit resident statements with reviewed spelling aliases only."""
    import re
    normalized = normalize_region(value)
    if not normalized:
        return False
    if re.search(r'제외|불가|불필요|아닌|아니며|대상\s*아님|통계|현황', quote):
        return False
    spellings = {normalized}
    parts = normalized.split(' ', 1)
    for group in _PROVINCES:
        if parts[0] == group[0]:
            spellings.update(alias + (' ' + parts[1] if len(parts) == 2 else '') for alias in group)
    spellings.update(_VERIFIED_DISTRICT_ALIASES.get(normalized, ()))
    for spelling in spellings:
        if spelling in _VERIFIED_DISTRICT_ALIASES.get(normalized, ()):
            if any(re.search(r'(?<![가-힣])' + re.escape(alias) + r'(?:\s|$)', quote)
                   for group in _PROVINCES if group[0] != parts[0] for alias in group):
                continue
        # Keep word boundaries; e.g. 인천광역시 중구 must not match 서울 중구.
        region_pattern = r'(?<![가-힣])' + r'\s*'.join(re.escape(part) for part in spelling.split())
        pattern = region_pattern + (r'(?:\s*(?:에\s*)?거주(?:자|민|하는|하시는|중|하고|\s|$)'
                    r'|\s*(?:구민|시민|주민)(?!센터|자치)|민\b)')
        if re.search(pattern, quote):
            return True
        # Stronger left boundary for the added path: an ASCII name/code is
        # not an independently named locality either. Existing matches stay
        # unchanged; only this explicit registration grammar gains support.
        registered_region = r'(?<![가-힣A-Za-z0-9])' + r'[ \t\u00a0]*'.join(
            re.escape(part) for part in spelling.split())
        if _registered_residence_matches(registered_region, quote):
            return True
    return False


def normalize_region(value):
    parts = value.split()
    if not parts:
        return ''
    # Never strip city/ward suffixes: 경기도 광주시 is not 광주광역시.
    parts[0] = _ALIASES.get(parts[0], parts[0])
    return ' '.join(parts)

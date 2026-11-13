"""Bounded, exact-source presentation for whole-source news. No I/O or AI calls.

Canonical support_detail remains untouched. Display metadata is never evidence
for classification: it is checked again against that canonical text on every read.
"""
from copy import deepcopy
import hashlib
import re

META_KEY = 'news_readable'
RESPONSE_KEY = '_readable_sections'
VERSION = 1
MAX_SECTIONS = 8
MAX_QUOTE_CHARS = 800
MAX_TOTAL_CHARS = 2400
SHORT_LOCAL_SOURCE_CHARS = 1000
_HEADINGS = dict(support='지원 내용', eligibility='대상·자격', application='신청 방법',
                 period='기간', notes='참고 사항')
_MARKER = re.compile(r'---\s*\[(?:첨부파일|게시글\s*본문|본문)', re.I)
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]')
_UNSAFE = re.compile(
    r'<|>|(?:https?://|www\.|javascript\s*:|data\s*:|file\s*:|mailto\s*:)|'
    r'\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b|'
    r'!?\[[^\]\n]*\]\s*(?:\(|\[|:)|&(?:#\d+|#x[0-9a-f]+|lt|gt|colon);|'
    r'ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions|system\s*prompt|'
    r'[`*_]|(?m:^\s*(?:#{1,6}\s|[-+]\s|\d+\.\s))|'
    r'fact_evidence|_readable_sections|source_support_selection|'
    r'(?:이전|위의|모든)\s*지시[^\n]{0,20}무시', re.I)
_NOISE = re.compile(
    r'^(?:개인정보\s*처리\s*방침|이용\s*약관|저작권\s*정책|사이트\s*맵|'
    r'본문\s*바로\s*가기|메뉴\s*바로\s*가기|로그인|로그아웃|회원가입|공유하기|인쇄하기|목록으로)'
    r'(?:\s*[|·/]\s*[^.\n]*)?[.\s]*$|copyright|all rights reserved|'
    r'첨부\s*(?:파일)?\s*(?:읽기|추출|분석|다운로드)\s*(?:실패|오류|상태|완료)|'
    r'\[업무연락처 생략\]|일부 내용 생략|source\.content', re.I)
_DEPENDENT = re.compile(r'^(?:단\s*[,，:]|다만|그러나|아님|아닌|아니며|아닙|않|'
                        r'이\s*경우|이때|해당\s*경우|제외(?:됩니다|합니다|됨|$)|불가(?:합니다|$)|'
                        r'(?:지원\s*|신청\s*)?대상(?:이|은|에)?\s*(?:아님|아닙|아닌)|'
                        r'(?:지원|신청|지급)\s*(?:하지|할\s*수|받을\s*수)\s*(?:않|없))')
_CONDITION = re.compile(
    r'지원\s*내용|지원\s*금액|혜택|대상|자격|조건|거주|이상|이하|미만|초과|한해|한하여|경우|다만|'
    r'제외|불가|중복|필수|제출|신청|접수|마감|기한|기간|까지|부터')
_LABEL = re.compile(r'^(?:[-•·]\s*)?(?:지원\s*내용|지원\s*금액|혜택|대상|지원\s*대상|'
                    r'신청\s*대상|신청\s*자격|자격|신청\s*방법|접수\s*방법|'
                    r'신청\s*기간|접수\s*기간|모집\s*기간|기간|유의\s*사항|참고\s*사항|'
                    r'제외\s*대상|제외\s*조건)\s*[:：]?\s*$')
_LABEL_START = re.compile(r'^(?:지원\s*내용|지원\s*금액|혜택|대상|지원\s*대상|신청\s*대상|'
                          r'신청\s*자격|자격|신청\s*방법|접수\s*방법|신청\s*기간|접수\s*기간|'
                          r'모집\s*기간|기간|유의\s*사항|참고\s*사항|제외\s*대상|제외\s*조건)\s*[:：]')
_CHECK_NOTE = '정리된 내용을 안전하게 표시하기 어려워 본문을 생략했습니다. 원문에서 지원 내용과 신청 조건을 확인해주세요.'
_EXCERPT_NOTE = '원문에서 확인한 구절을 항목별로 표시했습니다. 전체 안내와 첨부파일은 원문에서 확인해주세요.'

PROMPT = '''
[Readable source passages: separate from canonical whole-source selection]
Also return _readable_sections: at most 8 objects {section, quote}.
section must be support|eligibility|application|period|notes. quote must copy a
COMPLETE verbatim SOURCE.content passage (including its label, line breaks,
conditions, exclusions and dependent sentences), never paraphrase or translate.
Keep source order and keep a restriction with its subject in the SAME quote.
Preserve all application/eligibility/period restrictions, including the tail.
For SOURCE.content at most 1000 characters, preserve EVERY complete non-noise
passage, including its title and contact text; use notes when no heading fits.
Each quote is at most 800 characters; all quotes together at most 2400.
Select only informative announcement passages: no menus/footer, attachment
processing status, links/HTML, invented citations or entire-source dumps.
If these conditions cannot be met, return []. Never shorten a passage to fit.
These presentation fields do not replace _support_source_units or any evidence.
'''


def response_schema(base):
    schema = deepcopy(base)
    schema['properties'][RESPONSE_KEY] = dict(type='array', maxItems=MAX_SECTIONS,
        items=dict(type='object', properties=dict(
            section=dict(type='string', enum=list(_HEADINGS)),
            quote=dict(type='string', minLength=8, maxLength=MAX_QUOTE_CHARS)),
            required=['section', 'quote'], additionalProperties=False))
    schema['required'].append(RESPONSE_KEY)
    return schema


def _sha(source):
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _source_units(source):
    """Whole sentences/lines, retaining adjacent labels and dependent clauses.

    OCR commonly produces one long physical line. Punctuation can separate its
    complete sentences, but numeric dates/decimals and URL periods cannot.
    All returned values are exact contiguous spans, including original spacing.
    """
    links = [match.span() for match in re.finditer(r'(?:https?://|www\.)\S+', source)]
    spans, cursor = [], 0
    boundaries = re.finditer(r'\r?\n|(?<!\d)[.!?;。](?!\d)(?=\s|$)', source)
    for boundary in boundaries:
        if any(start <= boundary.start() < end for start, end in links):
            continue
        end = boundary.start() if '\n' in boundary.group() else boundary.end()
        raw = source[cursor:end]
        if raw.strip():
            start = cursor + len(raw) - len(raw.lstrip())
            spans.append([start, end - len(raw) + len(raw.rstrip())])
        cursor = boundary.end()
    if source[cursor:].strip():
        raw = source[cursor:]
        spans.append([cursor + len(raw) - len(raw.lstrip()), len(source.rstrip())])
    grouped = []
    for start, end in spans:
        text = source[start:end]
        previous = source[grouped[-1][0]:grouped[-1][1]] if grouped else ''
        continuation = (previous and _LABEL_START.search(previous)
            and not previous.endswith(('.', '!', '?', ';', '。'))
            and not _LABEL_START.search(text) and not _LABEL.fullmatch(text)
            and not re.match(r'^[^:：\n]{1,30}[:：]', text)
            and not _NOISE.search(text) and not _MARKER.search(text))
        if grouped and (_LABEL.fullmatch(previous) or _DEPENDENT.search(text) or continuation):
            grouped[-1][1] = end
        else:
            grouped.append([start, end])
    for start, end in grouped:
        yield start, source[start:end]


def _complete_span(quote, source, after):
    """Do not sever a label, a sentence, or a wrapped negative predicate."""
    units = list(_source_units(source))
    starts = {start for start, _ in units}
    ends = {start + len(text) for start, text in units}
    for match in re.finditer(re.escape(quote), source):
        start, end = match.span()
        if start < after or start not in starts or end not in ends:
            continue
        first = quote.splitlines()[0].strip(' \t-•·')
        if _DEPENDENT.search(first):
            continue
        return start, end
    return None


def _validated_sections(sections, source):
    from services.news_fact_grounding import source_quote_issue
    from services.news_analysis_safety import _omitted_conditions
    if (not isinstance(source, str) or not source.strip() or len(source) > 30000
            or _CONTROL.search(source) or type(sections) is not list
            or not 1 <= len(sections) <= MAX_SECTIONS):
        return None
    output, end, total = [], 0, 0
    for item in sections:
        if (type(item) is not dict or set(item) != {'section', 'quote'}
                or type(item.get('section')) is not str or item['section'] not in _HEADINGS
                or type(item.get('quote')) is not str):
            return None
        quote = item['quote']
        total += len(quote)
        if (quote != quote.strip() or not 8 <= len(quote) <= MAX_QUOTE_CHARS
                or total > MAX_TOTAL_CHARS or _UNSAFE.search(quote) or _NOISE.search(quote)
                or _MARKER.search(quote) or quote not in source or source_quote_issue(quote, source)):
            return None
        span = _complete_span(quote, source, end)
        if span is None:
            return None
        end = span[1]
        output.append(dict(section=item['section'], quote=quote))
    text = '\n'.join(item['quote'] for item in output)
    if (len(source.strip()) > 1000 and total >= len(source.strip()) * .85
            or len(output) == 1 and output[0]['quote'] == source.strip()
            and ('\n' in source.strip() or len(source.strip()) > 180)):
        return None
    if _omitted_conditions(dict(support_detail=text, eligibility_detail=''), source):
        return None
    # Short sources need complete coverage, not a keyword-selected subset:
    # benefits and recipient limits may have no label or expected condition word.
    # Keep the existing conservative condition guard for longer source excerpts.
    short_source = len(source) <= SHORT_LOCAL_SOURCE_CHARS
    for _, unit in _source_units(source):
        if ((short_source or _CONDITION.search(unit))
                and not _NOISE.search(unit) and not _MARKER.search(unit)
                and unit not in text):
            return None
    return output


def attach(analysis, sections):
    """Attach only locally validated metadata; malformed optional prose is soft-fail."""
    analysis.pop(META_KEY, None)
    source = analysis.get('support_detail')
    checked = _validated_sections(sections, source)
    if checked:
        analysis[META_KEY] = dict(version=VERSION, source_sha256=_sha(source), sections=checked)
    return analysis


def _local_sections(source):
    if isinstance(source, str) and len(source) <= SHORT_LOCAL_SOURCE_CHARS:
        # Never let label-only candidates bypass complete short-source coverage.
        return _short_local_sections(source)
    candidates = []
    for _, quote in _source_units(source):
        if _MARKER.search(quote) or _NOISE.search(quote):
            continue
        label = quote.splitlines()[0].split(':', 1)[0].split('：', 1)[0].strip(' \t-•·')
        if re.search(r'신청\s*방법|접수\s*방법', label):
            section = 'application'
        elif re.search(r'기간|마감|기한', label):
            section = 'period'
        elif re.search(r'대상|자격|조건', label):
            section = 'eligibility'
        elif re.search(r'지원\s*내용|지원\s*금액|혜택', label):
            section = 'support'
        elif re.search(r'유의\s*사항|참고\s*사항', label):
            section = 'notes'
        else:
            continue
        candidates.append(dict(section=section, quote=quote))
    checked = _validated_sections(candidates, source)
    return checked or _short_local_sections(source)


def _short_local_sections(source):
    """Recover short, complete conditions when optional display quotes are absent.

    No paraphrase, prefix clipping or permission change: the same exact-span,
    markup, omission and provenance checks still own acceptance. Long OCR and
    uncertain multipart documents must not become an automatic source dump.
    """
    if not isinstance(source, str) or len(source) > SHORT_LOCAL_SOURCE_CHARS:
        return None
    candidates = []
    for start, quote in _source_units(source):
        if _MARKER.search(quote) or _NOISE.search(quote):
            continue
        if re.search(r'기간|마감|기한', quote):
            section = 'period'
        elif re.search(r'대상|자격|거주|(?:개인|기관|보호자|대리인)(?:이|만|도)?\s*신청', quote):
            section = 'eligibility'
        elif re.search(r'신청|접수|제출', quote):
            section = 'application'
        elif re.search(r'지원\s*내용|지원\s*금액|혜택', quote):
            section = 'support'
        else:
            section = 'notes'
        # Adjacent sentences with the same heading may share a section, but
        # never jump over an omitted line, processing marker or footer.
        if (candidates and candidates[-1]['section'] == section
                and not source[candidates[-1]['end']:start].strip()
                and start + len(quote) - candidates[-1]['start'] <= MAX_QUOTE_CHARS):
            candidates[-1]['end'] = start + len(quote)
            candidates[-1]['quote'] = source[candidates[-1]['start']:candidates[-1]['end']]
        else:
            candidates.append(dict(section=section, quote=quote, start=start, end=start + len(quote)))
    return _validated_sections([
        dict(section=item['section'], quote=item['quote']) for item in candidates], source)


def _render(sections):
    # Consumers use a deliberately small Markdown subset. Reject quote markup
    # above instead of adding backslashes they would display as literal text.
    return '\n\n'.join('### ' + _HEADINGS[item['section']] + '\n' + item['quote']
                         for item in sections)


def presentation(analysis, *, whole_source=False):
    """Project UI/export prose without altering source, evidence or decisions."""
    if not isinstance(analysis, dict):
        return dict(support_detail='', presentation_kind='legacy', source_note='')
    source = analysis.get('support_detail')
    source = source if isinstance(source, str) else ''
    selected = analysis.get('source_support_selection')
    is_whole = (whole_source or 'source_support_selection' in analysis
                or META_KEY in analysis or bool(_MARKER.search(source)))
    if not is_whole:
        return dict(support_detail=source, presentation_kind='legacy', source_note='')
    withheld = dict(support_detail='', presentation_kind='source_excerpt', source_note=_CHECK_NOTE)
    if not source or len(source) > 30000 or _CONTROL.search(source):
        return withheld
    if 'source_support_selection' in analysis:
        if (not isinstance(selected, dict) or selected.get('source_sha256') != _sha(source)
                or type(selected.get('version')) is not int or selected['version'] not in (1, 2, 3)
                or selected.get('unit_ids') != [1]
                or type(selected['unit_ids'][0]) is not int):
            return withheld
        if type(selected.get('part_count', 1)) is not int or selected.get('part_count', 1) != 1:
            return dict(withheld, source_note='여러 구간으로 나누어 분석한 자료입니다. 조건의 연결 관계와 전체 내용을 원문에서 확인해주세요.')
    if META_KEY in analysis:
        meta = analysis[META_KEY]
        if (not isinstance(meta, dict) or set(meta) != {'version', 'source_sha256', 'sections'}
                or type(meta.get('version')) is not int or meta['version'] != VERSION
                or meta.get('source_sha256') != _sha(source)):
            return withheld
        sections = _validated_sections(meta['sections'], source)
        if not sections:
            return withheld
        return dict(support_detail=_render(sections), presentation_kind='structured',
                    source_note=_EXCERPT_NOTE)
    sections = _local_sections(source)
    if sections:
        return dict(support_detail=_render(sections), presentation_kind='source_excerpt',
                    source_note=_EXCERPT_NOTE)
    return withheld

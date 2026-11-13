"""Narrow presentation equivalences; never join arbitrary Korean sentences.

Labels keep their values, URLs keep their application-channel context, and
negative predicates keep their line boundaries. No facts are added or removed.
"""
import re

_LABELS = frozenset({
    '모집대상', '지원대상', '신청대상', '참여대상', '신청자격',
    '지원금액', '지원내용', '신청방법', '접수방법', '신청권자',
    '신청기간', '접수기간', '모집기간', '지급일', '지급방식',
})
_URL = re.compile(r'(?:https?://|www\.)[^\s<>()]+')
_BOUNDARY = re.compile(r'\n|(?<!\d)\.(?!\d)|;')


def _label(line):
    return re.sub(r'\s+', '', line.strip(' \t-•·#*:：✅📋📍💰📅📝'))


def presentation_clauses(text):
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    # Only an adjacent label and its first value; never consume a blank line,
    # another label, or a free-form continuation/negative predicate.
    lines = text.split('\n')
    joined = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (_label(line) in _LABELS and index + 1 < len(lines)
                and not re.match(r'\s*(?:#{1,6}\s|[✅📋📍💰📅📝])', line)
                and lines[index + 1].strip()
                and _label(lines[index + 1]) not in _LABELS
                and not re.match(r'\s*(?:#{1,6}\s|[✅📋📍💰📅📝])', lines[index + 1])
                and not re.match(r'\s*[^:：]{1,30}[:：]', lines[index + 1])):
            line = line.rstrip(' :：') + ': ' + lines[index + 1].lstrip()
            index += 1
        joined.append(line)
        index += 1
    text = '\n'.join(joined)
    # A standalone parenthesized link immediately following its website label
    # is a wrapped link, not a second sentence. Keep it bound to that label.
    text = re.sub(r'(웹사이트|홈페이지|누리집)[ \t]*\n[ \t]*(\(https?://[^\s<>()]+\))',
                  r'\1 \2', text)
    # Preserve the independent mobile application channel as its own clause.
    # Do not split arbitrary suffixes such as "아님", "제외" or "불가".
    text = re.sub(r'(\(https?://[^\s<>()]+\))[ \t]+(?=모바일\s*앱\s*신청(?:\s|$))',
                  r'\1\n', text)
    spans = [(match.start(), match.end()) for match in _URL.finditer(text)]
    start = 0
    for match in _BOUNDARY.finditer(text):
        if any(left <= match.start() < right for left, right in spans):
            continue  # periods and semicolons inside a URL are not sentences
        yield text[start:match.start()]
        start = match.end()
    yield text[start:]

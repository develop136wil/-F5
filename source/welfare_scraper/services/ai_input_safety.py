"""Small, local preflight for obvious sensitive text before an AI request.

This is not a complete personal-data detector. It neither anonymizes input nor
decides that a person consented to its transmission. Callers retain the original
text and stop before quota reservation/provider dispatch when review is needed.
Only fixed reason codes escape this module; never matches, positions or snippets.
Images, PDFs and arbitrary objects are not inspected by this text-only check.
"""
from dataclasses import dataclass
import datetime
import re
import unicodedata


REVIEW_MESSAGE = (
    '개인 연락처·식별번호 또는 비밀키로 보이는 내용이 있어 AI 전송을 보류했습니다. '
    '입력과 원문을 직접 확인한 뒤 민감한 내용을 제외하고 다시 시도해주세요.'
)
_REASON_ORDER = ('identity_number', 'mobile_number', 'email_address', 'credential', 'uninspected_input')


@dataclass(frozen=True)
class InputSafetyResult:
    """No source text or match details are retained, even in repr()."""
    allowed: bool
    reasons: tuple[str, ...] = ()


class AiInputReviewRequired(RuntimeError):
    code = 'AI_INPUT_REVIEW_REQUIRED'

    def __init__(self, reasons=()):
        self.reasons = tuple(reason for reason in _REASON_ORDER if reason in reasons)
        super().__init__(REVIEW_MESSAGE)


_PHONE = re.compile(
    r'(?<!\d)(?:0(?:10|11|16|17|18|19)|\+82[ .-]?(?:\(0\)[ .-]?)?1[016789])'
    r'[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)'
)
_IDENTITY = re.compile(r'(?<!\d)(\d{6})([ -]?)([0-9])(\d{6})(?!\d)')
_IDENTITY_LABEL = re.compile(r'주민\s*(?:등록\s*)?번호|외국인\s*등록\s*번호')
_AMOUNT_SUFFIX = re.compile(r'\s*(?:원|만원|억원|천원|KRW|USD|달러|엔|유로)(?![A-Za-z])', re.I)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64})@"
    r'((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,10}[A-Za-z]{2,63})'
    r'(?![A-Za-z0-9-])'
)
_PUBLIC_ROLES = frozenset({'info', 'contact', 'help', 'helpdesk', 'support', 'welfare', 'center'})
_PUBLIC_DOMAIN_SUFFIXES = ('.go.kr', '.or.kr', '.ac.kr')
_KNOWN_CREDENTIAL = re.compile(
    r'AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])'
    r'|(?<![0-9A-Za-z_])(?:ntn_|secret_)[0-9A-Za-z_-]{20,}'
    r'|(?<![0-9A-Za-z_])(?:gh[pousr]_|github_pat_)[0-9A-Za-z_]{20,}'
    r'|(?<![0-9A-Za-z])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])'
    r'|(?<![0-9A-Za-z_])sk-(?:proj-|ant-)?[0-9A-Za-z_-]{20,}'
    r'|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
    r'|(?:https?://)?(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d{8,}/[0-9A-Za-z_-]{12,}',
    re.I,
)
_CREDENTIAL_VALUE = re.compile(
    r'''(?ix)(?:["']?(?:google_api_key|notion_token|welfare_desktop_token|x-goog-api-key|api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|password|authorization|token)["']?\s*[:=]\s*["']?(?:Bearer\s+)?|\bBearer\s+)([A-Za-z0-9_.~+/%=-]{12,4096})'''
)
_URL_CREDENTIAL = re.compile(
    r'(?i)[?&](?:key|api[_-]?key|token|access_token|secret|password|signature)=([A-Za-z0-9_.~+/%=-]{12,4096})'
)
_URL_PASSWORD = re.compile(r'(?i)https?://[^\s/@:]{1,128}:[^\s/@]{1,256}@')
_PLACEHOLDER = re.compile(r'(?i)^(?:YOUR_(?:API_KEY|TOKEN|SECRET|PASSWORD)(?:_HERE)?|REPLACE_WITH_(?:API_KEY|TOKEN)|INSERT_(?:API_KEY|TOKEN)_HERE|[x*_\-.]+)$')
_NORMALIZE_EXTRA = str.maketrans({'\u200b': '', '\u200c': '', '\u200d': '', '\ufeff': '', '\u2060': '', '\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2013': '-', '\u2014': '-', '\u2212': '-'})


def _has_identity_number(text):
    for match in _IDENTITY.finditer(text):
        birth, separator, first, _tail = match.groups()
        century = 2000 if first in '3478' else 1800 if first in '09' else 1900
        try:
            datetime.date(century + int(birth[:2]), int(birth[2:4]), int(birth[4:6]))
        except ValueError:
            continue
        # A bare 13-digit amount is not, by itself, an identity number. A
        # separated identity form or a nearby explicit label remains reviewable.
        if (not separator and _AMOUNT_SUFFIX.match(text[match.end():])
                and not _IDENTITY_LABEL.search(text[max(0, match.start() - 24):match.start()])):
            continue
        return True
    return False


def _is_public_role_email(local, domain):
    # Domain alone does not distinguish staff personal mail from a shared box.
    return local.casefold() in _PUBLIC_ROLES and domain.casefold().endswith(_PUBLIC_DOMAIN_SUFFIXES)


def inspect_ai_text(text) -> InputSafetyResult:
    """Inspect without modifying the caller's text or returning sensitive data.

    Ambiguous mobile/free-mail contacts are held even if publicly posted; a
    published contact is not automatically permission to send it to an AI.
    A clean result only means these limited text patterns were not found.
    """
    if not isinstance(text, str):
        return InputSafetyResult(False, ('uninspected_input',))
    normalized = unicodedata.normalize('NFKC', text).translate(_NORMALIZE_EXTRA)
    found = set()
    if _has_identity_number(normalized):
        found.add('identity_number')
    if _PHONE.search(normalized):
        found.add('mobile_number')
    if any(not _is_public_role_email(*match.groups()) for match in _EMAIL.finditer(normalized)):
        found.add('email_address')
    if (_KNOWN_CREDENTIAL.search(normalized) or _URL_PASSWORD.search(normalized)
            or any(not _PLACEHOLDER.fullmatch(match[1].rstrip('.'))
                   for pattern in (_CREDENTIAL_VALUE, _URL_CREDENTIAL)
                   for match in pattern.finditer(normalized))):
        found.add('credential')
    reasons = tuple(reason for reason in _REASON_ORDER if reason in found)
    return InputSafetyResult(not reasons, reasons)


def ensure_ai_text_safe(*texts) -> None:
    """Raise a value-free error before dispatch; never mask or rewrite input.

    Pass the actual text payloads, including source/free-input fields and URLs,
    before reserving a quota slot. Do not pass binary attachments as str(bytes).
    Binary review/consent must be handled separately by the caller.
    """
    reasons = {reason for text in texts for reason in inspect_ai_text(text).reasons}
    if not texts:
        reasons.add('uninspected_input')
    if reasons:
        raise AiInputReviewRequired(reasons)

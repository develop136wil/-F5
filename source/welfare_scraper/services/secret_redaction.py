"""Redact credentials in diagnostics without reading configuration files."""
import logging
import os
import re
from urllib.parse import quote, quote_plus


_HIDDEN = '[REDACTED]'
_SECRET_NAMES = ('GOOGLE_API_KEY', 'NOTION_TOKEN', 'DISCORD_WEBHOOK_URL',
                 'GOOGLE_CHAT_WEBHOOK_URL', 'WELFARE_DESKTOP_TOKEN')
_TOKEN_PATTERN = re.compile(r'AIza[0-9A-Za-z_-]{35}|(?:ntn_|secret_)[0-9A-Za-z_-]{20,}')
_QUERY_PATTERN = re.compile(
    r'(?i)([?&](?:key|api[_-]?key|token|access_token|auth|authorization|secret|password|signature)=)[^&#\s\'"<>]*')
_ASSIGNMENT_PATTERN = re.compile(
    r'''(?ix)((?:['"])?(?:google_api_key|notion_token|welfare_desktop_token|x-goog-api-key|api[_-]?key|access_token|authorization|password|secret|token)(?:['"])?\s*[:=]\s*['"]?)(?!\[REDACTED\])(?:Bearer\s+)?[^\s,;'")}\]&]+''')
_BEARER_PATTERN = re.compile(r'(?i)\bBearer\s+[^\s,;\'"<>]+')
_WEBHOOK_PATTERN = re.compile(
    r'''(?i)((?:https?://)?(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[^/\s'"?<>]+/)[^/?\s'"<>]+''')
_USERINFO_PATTERN = re.compile(r'(?i)(https?://)[^/\s@]+@')


def redact_secrets(value, *, secrets=None):
    text = str(value)
    values = secrets if secrets is not None else (os.environ.get(name, '') for name in _SECRET_NAMES)
    for secret in sorted({str(value) for value in values if value}, key=len, reverse=True):
        # Very short configured placeholders must not destroy ordinary diagnostic words.
        if len(secret) >= 6:
            for encoded in {secret, quote(secret, safe=''), quote_plus(secret)}:
                text = text.replace(encoded, _HIDDEN)
    text = _TOKEN_PATTERN.sub(_HIDDEN, text)
    text = _WEBHOOK_PATTERN.sub(lambda match: match[1] + _HIDDEN, text)
    text = _USERINFO_PATTERN.sub(lambda match: match[1] + _HIDDEN + '@', text)
    text = _QUERY_PATTERN.sub(lambda match: match[1] + _HIDDEN, text)
    text = _ASSIGNMENT_PATTERN.sub(lambda match: match[1] + _HIDDEN, text)
    return _BEARER_PATTERN.sub('Bearer ' + _HIDDEN, text)


class SecretRedactionFilter(logging.Filter):
    """Sanitize before any configured handler or parent handler sees the record."""
    def filter(self, record):
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


class SecretRedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_secrets(super().format(record))

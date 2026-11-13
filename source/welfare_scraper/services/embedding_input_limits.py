"""Local per-input admission for embedding; never truncate the source.

The supported gemini-embedding-001 input window is 2,048 tokens. Without an
exact local tokenizer, character counts cannot enforce that window. Use a much
more conservative UTF-8 byte ceiling with room left for framing. This is an app
safety limit, not an exact token count or a guarantee of provider acceptance.
It deliberately holds some inputs that the provider could otherwise accept.
Quota/TPM admission remains separate and must still run for every actual call.
"""

EMBEDDING_INPUT_MAX_BYTES = 1792
EMBEDDING_INPUT_LIMIT_VERSION = 'full-input-utf8-1792-v1'
EMBEDDING_INPUT_LIMIT_MESSAGE = (
    '입력이 의미 검색의 길이 보호값을 넘어 임베딩을 보류했습니다. '
    '원문은 자르지 않으며 단어 검색과 원문 확인은 계속 사용할 수 있습니다.'
)


class EmbeddingInputLimitExceeded(ValueError):
    code = 'EMBEDDING_INPUT_LIMIT_EXCEEDED'

    def __init__(self):
        super().__init__(EMBEDDING_INPUT_LIMIT_MESSAGE)


def embedding_input_fits(text: str, *, max_chars: int | None = None) -> bool:
    """Inspect a complete string locally without normalizing or retaining it."""
    if not isinstance(text, str):
        return False
    # Bound allocation before encoding very large inputs. Every UTF-8 character
    # occupies at least one byte; malformed surrogate text is never sent.
    if len(text) > EMBEDDING_INPUT_MAX_BYTES:
        return False
    if max_chars is not None and len(text) > max_chars:
        return False
    try:
        return len(text.encode('utf-8')) <= EMBEDDING_INPUT_MAX_BYTES
    except UnicodeEncodeError:
        return False


def require_embedding_input(text: str, *, max_chars: int | None = None) -> None:
    if not embedding_input_fits(text, max_chars=max_chars):
        raise EmbeddingInputLimitExceeded()

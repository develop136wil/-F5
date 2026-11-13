"""Code-owned page acquisition coverage. Never contains remote error text/URLs."""
LABELS = {
    'rendered_capture_partial': '화면 일부의 글자를 읽었습니다. 다른 영역이나 첨부파일에 빠진 정보가 없는지 확인해주세요.',
    'attachment_review': '첨부파일에서 읽지 못했거나 확인이 필요한 부분이 있습니다. 해당 부분을 원문과 비교해주세요.',
    'html_byte_limit': '페이지 용량 제한으로 일부 내용과 첨부 링크를 받지 못했을 수 있습니다.',
    'page_char_limit': '본문 길이 제한으로 뒷부분을 추출하지 못했습니다.',
    'decode_error': '페이지의 일부 글자를 해석하지 못했습니다.',
    'fetch_failed': '처음 페이지를 불러올 때 실패했습니다. 보강된 내용이 있더라도 원문을 확인해주세요.',
    'no_text': '처음 페이지를 읽을 때 본문 글자를 찾지 못했습니다. 첨부나 보강 내용 외에 빠진 정보가 없는지 확인해주세요.',
}
RECIPE = 'page-coverage-v1'
METHODS = frozenset({'local_ocr'})


def normalize(value):
    if not isinstance(value, dict):
        return {}
    reasons = value.get('reasons')
    if not isinstance(reasons, list):
        return {}
    reasons = list(dict.fromkeys(reason for reason in reasons[:10]
                                if isinstance(reason, str) and reason in LABELS))
    result = {'recipe': RECIPE, 'status': 'review_required' if reasons else 'complete',
              'reasons': reasons, 'note': ' '.join(LABELS[reason] for reason in reasons)}
    methods = value.get('methods')
    if isinstance(methods, list):
        known = sorted({method for method in methods[:10] if isinstance(method, str) and method in METHODS})
        if known:
            result['methods'] = known
    return result


def make(*reasons):
    return normalize({'reasons': list(reasons)})


def with_reasons(value, *reasons):
    known = normalize(value)
    return normalize({**known, 'reasons': [*known.get('reasons', []), *reasons]})


def with_methods(value, *methods):
    known = normalize(value)
    if not known:
        return {}  # An OCR method does not establish unknown page coverage.
    return normalize({**known, 'reasons': known.get('reasons', []),
                      'methods': [*known.get('methods', []), *methods]})


def input_note(analysis):
    if not isinstance(analysis, dict):
        return ''
    notes = []
    if analysis.get('news_import_review') is True:
        notes.append('대상 조건의 근거가 부족한 자료입니다. 지원 자격을 원문에서 확인해주세요.')
    page = normalize(analysis.get('source_page'))
    if page.get('status') == 'review_required':
        notes.append(page['note'])
    coverage = analysis.get('source_input')
    if isinstance(coverage, dict) and coverage.get('status') in ('partial', 'empty'):
        notes.append(str(coverage.get('note') or 'AI가 전체 내용을 확인하지 못했습니다.')[:1000])
    minimized = coverage.get('contact_minimization') if isinstance(coverage, dict) else None
    if isinstance(minimized, dict):
        from services.news_contact_input import RECIPE as CONTACT_RECIPE, NOTE as CONTACT_NOTE
        if (minimized.get('recipe') == CONTACT_RECIPE and minimized.get('status') == 'minimized'
                and not any(CONTACT_NOTE in note for note in notes)):
            notes.append(CONTACT_NOTE)
    return ' '.join(notes)[:1500]

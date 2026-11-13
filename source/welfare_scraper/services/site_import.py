"""Parse user-facing address sheets, without registering or contacting any site."""
import re
from urllib.parse import urlsplit

HEADERS = ('사이트 이름 (선택)', '게시판 주소 (필수)')


def column_key(value):
    value = re.sub(r'\([^)]*\)', '', str(value).replace('\ufeff', '')).strip().lower()
    value = re.sub(r'\s+', '', value)
    if value in {'url', '주소', '게시판주소', '사이트주소', '홈페이지주소'}:
        return 'url'
    if value in {'name', '사이트이름', '사이트명', '기관명', '이름'}:
        return 'name'
    return None


def read_address_file(path, extension):
    import pandas as pd
    if extension == '.csv':
        try:
            frame = pd.read_csv(path, encoding='utf-8-sig', dtype=str, keep_default_na=False, nrows=5001, skip_blank_lines=False)
        except UnicodeDecodeError:
            frame = pd.read_csv(path, encoding='cp949', dtype=str, keep_default_na=False, nrows=5001, skip_blank_lines=False)
    else:
        frame = pd.read_excel(path, dtype=str, keep_default_na=False, nrows=5001)
    return parse_addresses(frame)


def parse_addresses(frame):
    if len(frame) > 5000:
        raise ValueError('사이트 파일은 5,000행 이하로 나눠주세요.')
    columns = {}
    for col in frame.columns:
        key = column_key(col)
        if key:
            if key in columns:
                raise ValueError('주소 또는 사이트 이름 열이 두 개 이상입니다. 한 열씩만 남겨주세요.')
            columns[key] = col
    if 'url' not in columns:
        raise ValueError('첫 행에 게시판 주소 (필수) 또는 URL 열을 넣어주세요.')
    records, seen, errors = [], set(), []
    duplicates = 0
    for number, (_, row) in enumerate(frame.fillna('').iterrows(), 2):
        url = str(row[columns['url']]).strip()
        name = str(row[columns['name']]).strip() if 'name' in columns else ''
        if not url and not name:
            continue
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in {'http', 'https'} and parts.hostname
                     and not parts.username and not parts.password
                     and not re.search(r'\s', url))
            parts.port  # Reject malformed ports too.
        except ValueError:
            valid = False
        if not valid:
            errors.append(f'{number}행: http:// 또는 https://로 시작하는 게시판 주소를 입력해주세요.')
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        records.append({'url': url, 'name': name})
    if errors:
        extra = f' 외 {len(errors)-8}행' if len(errors) > 8 else ''
        raise ValueError('아직 등록하지 않았습니다. ' + ' / '.join(errors[:8]) + extra)
    if not records:
        raise ValueError('등록할 주소가 없습니다. 제목 아래에 실제 게시판 주소를 입력해주세요.')
    return records, duplicates

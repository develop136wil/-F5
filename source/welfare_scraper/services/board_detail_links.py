"""Known public board navigation, parsed as data; never execute supplied JS.

JS conversion supports only the observed DBWC and three Dobong welfare boards.
Generic literal links retain board identity; unknown JS stays unresolved.
"""
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, unquote

_ID = r'[1-9][0-9]{0,17}'
_CALL = re.compile(
    rf"(?:javascript:\s*)?(?:void\s+)?(go_view|modify|moveToView)\(\s*"
    rf"(?:'({_ID})'|\"({_ID})\"|({_ID}))\s*\)\s*;?\s*(?:return\s+false\s*;?)?",
    re.ASCII,
)
_BOARDS = {
    '/dobongBokji/bbs/BMSR00001/list.do': ('notice', '200098'),
    '/dobongBokji/bbs/BMSR00002/list.do': ('news', '200099'),
    '/dobongBokji/pgm/progrmReqst/list.do': ('program', '200086'),
}


def _url(value):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r'[\x00-\x20\\]', value):
        return None
    try:
        parsed = urlsplit(value)
        decoded_path = unquote(parsed.path)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or re.search(r'/(?:\.{1,2})(?:/|$)|[\\\x00-\x20]', decoded_path)
                or re.search(r'%2f|%5c', parsed.path, re.I)
                or parsed.port not in (None, 443 if parsed.scheme == 'https' else 80)):
            return None
        return parsed
    except ValueError:
        return None


def board_kind(value):
    """A list-page allowlist, not a generic same-domain URL guess."""
    parsed = _url(value)
    if parsed is None:
        return ''
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.hostname in ('www.dbwc2017.or.kr', 'dbwc2017.or.kr'):
        if (parsed.path == '/site/notice_02.php'
                and query.get('bm_id', ['b02']) == ['b02']
                and query.get('bmode', ['list']) == ['list']):
            return 'dbwc'
    if parsed.hostname == 'welfare.dobong.go.kr' and parsed.path in _BOARDS:
        kind, menu = _BOARDS[parsed.path]
        if query.get('menuNo', [menu]) == [menu]:
            return kind
    return ''


def _context(site_url, page_url):
    kind = board_kind(site_url)
    if not kind or board_kind(page_url) != kind:
        return None
    site, page = _url(site_url), _url(page_url)
    if (site.hostname.removeprefix('www.') != page.hostname.removeprefix('www.')
            or site.path != page.path):
        return None
    return kind, page


def resolve_script_detail(site_url, page_url, href='', onclick=''):
    """Map a single allowlisted function with a positive decimal ID to a GET URL."""
    context = _context(site_url, page_url)
    if context is None or not isinstance(href, str) or not isinstance(onclick, str):
        return ''
    calls = []
    for value in (href, onclick):
        if len(value) > 256:
            return ''
        value = value.strip()
        if value in ('', '#', 'javascript:;', 'javascript:void(0)', 'javascript:void(0);'):
            continue
        match = _CALL.fullmatch(value)
        if not match:
            return ''
        calls.append((match[1], next(part for part in match.groups()[1:] if part)))
    if not calls or any(call != calls[0] for call in calls):
        return ''
    function, identity = calls[0]
    kind, page = context
    if kind == 'dbwc' and function == 'go_view':
        path = page.path
        query = dict(bmode='view', bm_id='b02', idx=identity)
    elif kind in ('notice', 'news') and function == 'modify':
        path = page.path.removesuffix('list.do') + 'view.do'
        query = dict(menuNo=_BOARDS[page.path][1], boardId=identity)
    elif kind == 'program' and function == 'moveToView':
        path = page.path.removesuffix('list.do') + 'view.do'
        query = dict(menuNo='200086', reqstId=identity)
    else:
        return ''
    return urlunsplit((page.scheme, page.netloc, path, urlencode(query), ''))


def _program_detail(site_url, page_url, link):
    context = _context(site_url, page_url)
    if context is None or context[0] != 'program' or not isinstance(link, str):
        return ''
    page = context[1]
    detail = _url(urljoin(page_url, link))
    if (detail is None or detail.scheme != page.scheme or detail.netloc != page.netloc
            or detail.path != page.path.removesuffix('list.do') + 'view.do'):
        return ''
    query = parse_qs(detail.query, keep_blank_values=True)
    if (set(query) != {'menuNo', 'reqstId'} or query['menuNo'] != ['200086']
            or len(query['reqstId']) != 1 or not re.fullmatch(_ID, query['reqstId'][0], re.ASCII)):
        return ''
    return urlunsplit((page.scheme, page.netloc, detail.path,
                      urlencode(dict(menuNo='200086', reqstId=query['reqstId'][0])), ''))


def materialize_board_links(rows, site_url):
    """Shared by fallback and selector readers; internal DOM hints never escape."""
    output = []
    for raw in rows:
        row = {key: value for key, value in raw.items() if not key.startswith('_board_')}
        if '_board_page' in raw:
            if raw.get('_board_card') is True:
                row['link'] = _program_detail(site_url, raw['_board_page'], raw.get('link', ''))
            elif raw.get('_board_script') is True:
                row['link'] = resolve_script_detail(site_url, raw['_board_page'],
                                                   raw.get('_board_href', ''), raw.get('_board_onclick', ''))
        output.append(row)
    return output


# Only identifiers present on the registered list bind the generic board.
# Dobong's boardId on view.do is an article ID, absent from its list URL.
_BOARD_KEYS = frozenset(('bbsid', 'bbsno', 'boardid', 'boardno', 'boardcode',
                         'bbscode', 'bm_id', 'bo_table', 'code', 'bid'))
_MENU_KEYS = frozenset(('menuno', 'menu', 'mid'))
_PAGE_KEYS = frozenset(('page', 'pageindex', 'pageno', 'pagenum', 'currentpage',
                        'curpage', 'cpage', 'page_no', 'page_num', 'page_index',
                        'offset', 'article.offset'))


def _query(parsed):
    query = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        key = key.lower()
        if key in query or len(values) != 1:
            return None
        query[key] = values[0]
    return query


def _same_host(left, right):
    return (left.hostname.removeprefix('www.') == right.hostname.removeprefix('www.')
            and not (left.scheme == 'https' and right.scheme != 'https'))


def article_in_board(site_url, link):
    """News-only boundary: a same-host detail is not necessarily this board."""
    site, detail = _url(site_url), _url(link)
    if site is None or detail is None or not _same_host(site, detail):
        return False
    before, after = _query(site), _query(detail)
    if before is None or after is None:
        return False
    kind = board_kind(site_url)
    if kind == 'dbwc':
        return detail.path == site.path and after.get('bm_id') == 'b02'
    if kind in ('notice', 'news', 'program'):
        return (detail.path == site.path.removesuffix('list.do') + 'view.do'
                and after.get('menuno') == _BOARDS[site.path][1])
    for key in _BOARD_KEYS:
        if before.get(key) and after.get(key) != before[key]:
            return False
    for key in _MENU_KEYS:
        if before.get(key) and key in after and after[key] != before[key]:
            return False
    # Common list.do/view.do, board.php?bo_table=..., /news/slug route families.
    if detail.path == site.path:
        return True
    path = site.path.rstrip('/')
    leaf = path.rsplit('/', 1)[-1]
    if '.' in leaf or re.search(r'(?:list|List)$', leaf):
        namespace = path.rsplit('/', 1)[0] + '/'
    else:
        namespace = path + '/'
    if namespace == '/' and path:
        # A root list.do can use a root view.do, but not a neighbouring
        # /jobs/view.do. Generic /list routes also commonly use /post/<id>.
        return (detail.path.count('/') == 1
                or (leaf.lower() == 'list' and detail.path.startswith(('/post/', '/article/'))))
    return bool(namespace and detail.path.startswith(namespace))


def _stable_list_query(parsed):
    query = _query(parsed)
    if query is None:
        return None
    stable = {key: value for key, value in query.items()
              if value and key not in _PAGE_KEYS and not key.startswith('utm_')}
    if board_kind(urlunsplit(parsed)) == 'dbwc':
        stable.setdefault('bm_id', 'b02')
        if stable.get('bmode') == 'list':
            stable.pop('bmode')
    return stable


def list_page_in_board(site_url, page_url):
    """A pager may change a page number, not path, board, filters or host."""
    site, page = _url(site_url), _url(page_url)
    return bool(site and page and _same_host(site, page) and site.path == page.path
                and _stable_list_query(site) is not None
                and _stable_list_query(site) == _stable_list_query(page))


def _page_position(parsed):
    query = _query(parsed)
    if query is None:
        return None
    values = [(key, value) for key, value in query.items() if key in _PAGE_KEYS]
    if not values:
        return '', 1
    if (len(values) != 1 or not re.fullmatch(r'[0-9]{1,7}', values[0][1])
            or (int(values[0][1]) == 0 and values[0][0] not in ('offset', 'article.offset'))):
        return None
    return values[0][0], int(values[0][1])


def _pager_link(site_url, current_url, row):
    href, onclick = row.get('href') or '', row.get('onclick') or ''
    if not isinstance(href, str) or not isinstance(onclick, str):
        return ''
    if href.strip().lower().startswith('javascript:') or href.strip() in ('', '#'):
        # Exact observed GET listFrm/pageIndex call, never arbitrary JS.
        if board_kind(site_url) not in ('notice', 'news', 'program'):
            return ''
        calls = []
        for value in (href.strip(), onclick.strip()):
            if value in ('', '#', 'javascript:;', 'javascript:void(0)', 'javascript:void(0);'):
                continue
            match = re.fullmatch(r'(?:javascript:\s*)?fnSubmitForm\(([1-9][0-9]{0,6})\)\s*;?\s*(?:return\s+false\s*;?)?', value)
            if not match:
                return ''
            calls.append(match[1])
        if not calls or any(call != calls[0] for call in calls):
            return ''
        parsed = _url(current_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query['pageIndex'] = [calls[0]]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ''))
    if onclick and not re.fullmatch(r'\s*(?:javascript:\s*)?return\s+false\s*;?\s*', onclick):
        return ''
    return urljoin(current_url, href)


def next_list_page(site_url, current_url, controls):
    """Select one proven next GET; ambiguous/unsupported pagers stay partial."""
    if not list_page_in_board(site_url, current_url) or not isinstance(controls, list):
        return {'state': 'unverified', 'url': ''}
    current = _page_position(_url(current_url))
    if current is None:
        return {'state': 'unverified', 'url': ''}
    candidates, forward, end_evidence = [], False, False
    for row in controls[:200]:
        if not isinstance(row, dict):
            continue
        label = str(row.get('label') or '').strip()
        number = int(label) if re.fullmatch(r'[0-9]{1,7}', label) else None
        if row.get('current') is True:
            end_evidence = True
            continue
        if row.get('disabled') is True:
            end_evidence |= row.get('next') is True
            continue
        is_forward = row.get('next') is True or (number is not None and (
            number > current[1] or current[0] in ('offset', 'article.offset')))
        if not is_forward:
            end_evidence |= number is not None
            continue
        forward = True
        link = _pager_link(site_url, current_url, row)
        if not list_page_in_board(site_url, link):
            continue
        target = _page_position(_url(link))
        if target is None or not target[0] or (current[0] and current[0] != target[0]):
            continue
        before = current[1] if current[0] else (0 if target[0] in ('offset', 'article.offset') else 1)
        step = target[1] - before
        if step != 1 and not (target[0] in ('offset', 'article.offset') and 0 < step <= 100):
            continue
        candidates.append((target[1], link))
    if candidates:
        return {'state': 'next', 'url': min(candidates)[1]}
    return {'state': 'unverified' if forward or not end_evidence else 'end', 'url': ''}

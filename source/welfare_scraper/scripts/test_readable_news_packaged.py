"""Exercise real frozen read/export endpoints using synthetic local news only.

Uses the disposable-profile lifecycle runner. No real API keys, sites, AI or
Notion writes; canonical evidence must survive every presentation read.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
import test_policy_list_packaged as policy_smoke
import test_desktop_executable as smoke


def check_readable(client, data, html_output=None):
    policy_smoke.check_list(client, data, html_output)
    body = ('교육비 지원 안내\n개인이 신청합니다.\n'
            '지원 내용: 청소년에게 교재비 5만원을 지원합니다.\n'
            '신청 방법: 센터 방문 접수\n'
            '유의 사항: 다른 사업과 중복 신청할 수 없습니다.\n'
            'COPYRIGHT SYNTHETIC CENTER.')
    quotes = [dict(section=section, quote=quote) for section, quote in zip(
        ('notes', 'eligibility', 'support', 'application', 'notes'), body.splitlines()[:5])]
    digest = hashlib.sha256(body.encode()).hexdigest()
    analysis = dict(item_type='event', support_detail=body, news_import_review=False,
        news_readable=dict(version=1, source_sha256=digest, sections=quotes),
        source_support_selection=dict(version=3, source_sha256=digest, unit_ids=[1], part_count=1))
    cases = [
        ('packagedreadable', body, analysis),
        ('packagedinvalid', body, dict(analysis, news_readable=dict(
            version=1, source_sha256='invalid', sections=quotes))),
        ('packagedlegacy', '### 지원 내용\n- 독서 프로그램\n### 신청 방법\n- 센터 방문', {'item_type': 'event'}),
        ('packagedpolicy', '--- [게시글 본문] ---\n담당자가 작성한 정책 설명입니다.', {'item_type': 'policy'}),
    ]
    from services.news_gallery import store_poster
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        ids = []
        for title, canonical, metadata in cases:
            link = 'https://example.invalid/' + title
            row = conn.execute('''INSERT INTO welfare_items
                (title,link,site_name,is_archived,ai_data,support_detail)
                VALUES (?,?,?,0,?,?)''',
                (title, link, '합성 시험 기관', json.dumps(metadata, ensure_ascii=False), canonical))
            ids.append(row.lastrowid)
            store_poster(conn, link, '')
        conn.commit()
    for (title, canonical, _), item_id in zip(cases, ids):
        detail = client.get(f'/api/data/{item_id}')
        detail.raise_for_status()
        value = detail.json()
        assert value['support_detail'] == canonical
        presentation = value['news_presentation']
        search = client.get('/api/search', params=dict(q=title))
        search.raise_for_status()
        found = next(item for item in search.json()['items'] if item['id'] == item_id)
        if title == 'packagedpolicy':
            assert 'news_presentation' not in found
        else:
            assert found['news_presentation'] == presentation
        output = client.post('/api/news/gallery-html', json={'ids': [item_id]}, timeout=30)
        if title == 'packagedpolicy':
            assert presentation['support_detail'] == canonical
            assert presentation['presentation_kind'] == 'legacy'
            # Policy prose is preserved, but policy-only rows are not news exports.
            assert output.status_code == 404
            continue
        output.raise_for_status()
        assert output.headers['X-WelfareF5-AI-Calls'] == '0'
        assert 'https://example.invalid/' + title in output.text
        if title == 'packagedreadable':
            assert 'COPYRIGHT' not in presentation['support_detail']
            assert 'COPYRIGHT' not in output.text
            for text in ('개인이 신청합니다.', '교재비 5만원', '센터 방문 접수', '중복 신청할 수 없습니다.'):
                assert text in presentation['support_detail'] and text in output.text
            assert '<strong>지원 내용</strong>' in output.text
        elif title == 'packagedinvalid':
            assert presentation['support_detail'] == ''
            assert 'COPYRIGHT' not in output.text and '교재비 5만원' not in output.text
        elif title == 'packagedlegacy':
            assert presentation['support_detail'] == canonical
            assert '<strong>지원 내용</strong>' in output.text and '<li>독서 프로그램</li>' in output.text
    with closing(sqlite3.connect(data / 'welfare.db')) as conn:
        for (_, canonical, _), item_id in zip(cases, ids):
            current = conn.execute('SELECT support_detail,notion_page_id FROM welfare_items WHERE id=?',
                                   (item_id,)).fetchone()
            assert current[0] == canonical and not current[1]
    assert client.get('/api/ai/status').json()['recorded_requests_today'] == 0
    print('PASS: packaged detail/search/HTML projection, invalid evidence blocked, legacy Markdown, canonical preserved, no AI/Notion', flush=True)


if __name__ == '__main__':
    smoke.check_news_gallery = check_readable
    smoke.run(sys.argv[1])

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { getHomepageRegistrationWarning, isLikelyHomepageUrl } from '../src/utils/siteRegistration.js';

test('only obvious home addresses need confirmation; query-based boards remain valid', () => {
    for (const url of ['https://example.org', 'https://example.org/', 'https://example.org/index.html', 'https://example.org/MAIN.DO#top', 'https://example.org/default.aspx']) {
        assert.equal(isLikelyHomepageUrl(url), true, url);
    }
    for (const url of ['https://example.org/board', 'https://example.org/board.do?menu=101', 'https://example.org/index.php?board=notice', 'https://example.org/?page=notice', 'https://example.org/posts/1', 'not a URL', 'ftp://example.org/']) {
        assert.equal(isLikelyHomepageUrl(url), false, url);
    }
});

test('mixed pasted addresses prompt only for homes and explain the reversible choice', () => {
    assert.equal(getHomepageRegistrationWarning(['https://example.org/notices']), '');
    const warning = getHomepageRegistrationWarning(['https://example.org/', 'https://other.org/board']);
    assert.match(warning, /대표 홈페이지로 보이는 주소 1개/);
    assert.match(warning, /https:\/\/example.org\//);
    assert.doesNotMatch(warning, /other.org/);
    assert.match(warning, /화면 자체가 게시판 목록/);
    assert.match(warning, /수집 확인 필요/);
});

test('registration uses consistent board wording and confirms before starting a mutation', () => {
    const source = readFileSync(new URL('../src/pages/Sites.jsx', import.meta.url), 'utf8');
    assert.match(source, /공지사항·새 소식 게시판 주소/);
    assert.match(source, /공지사항·모집 안내 게시판의 목록 주소를 입력하세요\. 여러 주소는 한 줄에 하나씩 입력하세요\./);
    assert.match(source, /grid grid-cols-1 items-stretch gap-4 sm:grid-cols-\[minmax\(0,1fr\)_10rem\]/);
    assert.match(source, /h-auto min-h-12 w-full self-stretch/);
    assert.doesNotMatch(source, /기관 홈페이지나 게시판 주소/);
    assert.match(source, /if \(homepageWarning && !window.confirm\(homepageWarning\)\) return;\s*registrationPending.current = true;\s*addSiteMutation.mutate/);
    assert.match(source, /게시글을 확인하지 못한 주소는 ‘수집 확인 필요’로 저장됩니다/);
    // Already registered home addresses also get guidance, without changing or
    // deleting the user's saved sites. Verified home-page boards are exempt.
    assert.match(source, /site.registration_status !== 'basic_verified' && isLikelyHomepageUrl\(site.url\)/);
});

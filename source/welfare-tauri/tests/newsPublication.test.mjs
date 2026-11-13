import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {publicationSettings, publicationLabel} from '../src/utils/newsPublication.js';

test('게시 기간 미설정과 기존 조건은 최근 30일을 기본값으로 사용한다', () => {
    assert.deepEqual(publicationSettings(), {
        publication_period: '30', publication_start_date: '', publication_end_date: '',
    });
    assert.deepEqual(publicationSettings({name:'기존 조건'}), publicationSettings());
    assert.equal(publicationLabel(), '최근 30일');
    assert.equal(publicationLabel({publication_period:'unknown'}), '최근 30일');
});

test('최근 7일·30일·90일 선택을 정확히 저장하고 수집일로 바꾸지 않는다', () => {
    for (const period of ['7','30','90']) {
        assert.equal(publicationSettings({publication_period:period}).publication_period, period);
        assert.equal(publicationLabel({publication_period:period}), `최근 ${period}일`);
    }
});

test('직접 지정은 시작·종료일과 같은 날을 포함하고 원래 초안을 수정하지 않는다', () => {
    const draft = Object.freeze({publication_period:'custom', publication_start_date:'2024-02-29', publication_end_date:'2024-03-01', name:'우리 기관'});
    assert.deepEqual(publicationSettings(draft), {
        publication_period:'custom', publication_start_date:'2024-02-29', publication_end_date:'2024-03-01',
    });
    assert.equal(publicationLabel(draft), '2024-02-29 ~ 2024-03-01');
    assert.equal(publicationSettings({...draft, publication_end_date:'2024-02-29'}).publication_end_date, '2024-02-29');
    assert.equal(draft.name, '우리 기관');
});

test('없는 날짜·비윤년·누락·0년·시간값은 직접 지정으로 저장하지 않는다', () => {
    for (const bad of ['', '2025-02-29', '2026-02-30', '2026-04-31', '2026-13-01', '2026-00-10', '0000-01-01', '2026-9-01', '2026-09-01T00:00:00Z', 'invalid']) {
        assert.throws(() => publicationSettings({publication_period:'custom', publication_start_date:bad, publication_end_date:'2026-12-31'}), /시작일과 종료일/, bad);
        assert.throws(() => publicationSettings({publication_period:'custom', publication_start_date:'2024-01-01', publication_end_date:bad}), /시작일과 종료일/, bad);
    }
});

test('역전 기간과 허용하지 않은 기간 값은 저장하지 않는다', () => {
    assert.throws(() => publicationSettings({publication_period:'custom', publication_start_date:'2026-10-01', publication_end_date:'2026-09-01'}), /시작일과 종료일/);
    for (const period of ['0','14','365','all','',30]) {
        assert.throws(() => publicationSettings({publication_period:period}), /수집 기간/);
    }
});

test('기본·별도 조건 모두 게시 기간을 검증하고 실행 전 게시일 기준을 표시한다', () => {
    const read = file => readFileSync(new URL(file, import.meta.url), 'utf8');
    for (const file of ['../src/components/NewsConditionWizard.jsx', '../src/components/NewsProfileEditor.jsx']) {
        assert.match(read(file), /publicationSettings\(draft\)/);
    }
    const run = read('../src/components/NewsProfileRunPanel.jsx');
    assert.match(run, /publicationLabel\(options.selection\)/);
    assert.match(run, /게시일 기준/);
});

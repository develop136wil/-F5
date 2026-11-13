import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';
import {newsTaskProgress, newsTaskPollInterval, compactNewsTaskStatus} from '../src/utils/newsTaskProgress.js';

const running = (phase, progress = {}, task = {}) => ({
    state:'running', stage:phase === 'analysis' ? 'analyzing' : 'collecting', active:true,
    progress:{schema_version:1, phase, ...progress}, ...task,
});
const source = relative => readFile(new URL(relative, import.meta.url), 'utf8');
const require = createRequire(import.meta.url);
const componentBundle = await build({
    entryPoints:[fileURLToPath(new URL('../src/components/NewsTaskProgress.jsx', import.meta.url))],
    bundle:true, write:false, platform:'node', format:'esm', logLevel:'silent',
    plugins:[{name:'existing-react-runtime', setup(builder) {
        builder.onResolve({filter:/^react$/}, () => ({path:pathToFileURL(require.resolve('react')).href, external:true}));
    }}],
});
const {default:NewsTaskProgress} = await import(`data:text/javascript;base64,${Buffer.from(componentBundle.outputFiles[0].text).toString('base64')}`);
const markup = (task, extra = {}) => renderToStaticMarkup(React.createElement(NewsTaskProgress, {task, ...extra}));

test('기관 진행률은 저장 후보가 아닌 확인한 사이트 수를 분모로 계산한다', () => {
    const view = newsTaskProgress(running('sites', {
        sites_processed:12, sites_total:42, documents_processed:380, attachments_processed:17,
        sites_review_required:2, site_name:'서울시 공지사항', saved_total:900,
    }));
    assert.equal(view.phaseLabel, '기관 게시판 확인');
    assert.equal(view.percent, 28);
    assert.equal(view.amount, '기관 확인 12/42개 사이트');
    assert.equal(view.siteName, '서울시 공지사항');
    assert.deepEqual(view.lines, ['본문 확인 380건','첨부 확인 17개','추가 확인 2개 사이트']);
    assert.match(view.phaseNote, /현재 단계.*전체 작업 완료율·남은 시간과는 다릅니다/);
    assert.equal(view.active, true);
});

test('본문 진행률은 현재 기관 자료 범위이고 사이트 누계와 구분한다', () => {
    const view = newsTaskProgress(running('content', {
        sites_processed:12, sites_total:42, site_documents_processed:3, site_documents_total:8,
        documents_processed:103, attachments_processed:9, site_name:'기관 게시판',
    }));
    assert.equal(view.phaseLabel, '본문·첨부 확인');
    assert.equal(view.percent, 37);
    assert.equal(view.amount, '이 기관 자료 3/8건');
    assert.deepEqual(view.lines, ['기관 확인 12/42개 사이트','본문 확인 103건','첨부 확인 9개']);
});

test('AI 분석 단계는 처리·잔여·보류·실패 수를 구분하고 이전 기관 이름을 숨긴다', () => {
    const view = newsTaskProgress(running('analysis', {
        analysis_processed:18, analysis_total:30, analysis_remaining:12,
        analysis_review_required:4, analysis_failed:2, sites_processed:42, sites_total:42,
        documents_processed:70, site_name:'이전 기관',
    }));
    assert.equal(view.phaseLabel, '수집 자료 분석');
    assert.equal(view.percent, 60);
    assert.equal(view.amount, '분석 처리 18/30건');
    assert.deepEqual(view.lines, ['남은 분석 12건','직접 확인 필요 4건','분석 실패 2건']);
    assert.equal(view.siteName, '');
});

test('분모가 미확인·잘못된 값이면 가짜 퍼센트를 만들지 않는다', () => {
    for(const total of [undefined, null, '12', -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1]) {
        const view = newsTaskProgress(running('sites', {sites_processed:0, sites_total:total}));
        assert.equal(view.percent, null, `total=${String(total)}`);
        assert.equal(view.active, true);
        assert.equal(view.phaseNote, '');
        assert.doesNotMatch(view.amount, /%|undefined|null|NaN|Infinity/);
    }
    const view = newsTaskProgress(running('sites', {sites_processed:0, sites_total:0}));
    assert.equal(view.percent, null);
    assert.equal(view.amount, '기관 확인 대상 없음');
});

test('처리 수가 잘못되거나 분모를 넘으면 100%로 보정해 완료를 오인시키지 않는다', () => {
    for(const done of [undefined, null, '4', -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1, 11]) {
        const view = newsTaskProgress(running('analysis', {analysis_processed:done, analysis_total:10}));
        assert.equal(view.percent, null, `done=${String(done)}`);
        assert.doesNotMatch(view.amount, /100%|NaN|Infinity/);
    }
    assert.equal(newsTaskProgress(running('analysis', {analysis_processed:0, analysis_total:10})).percent, 0);
    assert.equal(newsTaskProgress(running('analysis', {analysis_processed:10, analysis_total:10})).percent, 100);
});

test('실제로 보고된 단계의 분모가 없으면 다른 단계의 100%를 가져오지 않는다', () => {
    const base = {sites_processed:42, sites_total:42, documents_processed:500, attachments_processed:30};
    const content = newsTaskProgress(running('content', base));
    assert.equal(content.percent, null);
    assert.ok(content.lines.includes('기관 확인 42/42개 사이트'));
    const analysis = newsTaskProgress(running('analysis', base));
    assert.equal(analysis.percent, null);
    const unknown = newsTaskProgress(running('unknown', {...base, analysis_processed:100, analysis_total:100}));
    assert.equal(unknown.percent, null);
});

test('대기·실패·완료·중지는 stale active 플래그와 관계없이 움직이는 작업으로 표시하지 않는다', () => {
    const expected = {waiting:'잠시 멈춤', failed:'완료하지 못함', complete:'완료', cancelled:'사용자가 중지함'};
    for(const [state, title] of Object.entries(expected)) {
        const task = running('sites', {sites_processed:4, sites_total:8}, {state, active:true});
        const view = newsTaskProgress(task);
        assert.equal(view.active, false, state);
        assert.equal(view.title, title);
        assert.equal(view.percent, 50, '마지막 확인 수치는 보존한다');
        assert.doesNotMatch(markup(task), /animate-pulse/);
    }
});

test('과거 앱 일일 상한으로 멈춘 작업은 분석 수치를 보존하고 바로 재개를 안내한다', () => {
    const task = running('analysis', {analysis_processed:18, analysis_total:30, analysis_remaining:12}, {
        state:'waiting', error_code:'local_budget_exhausted', can_resume:true,
        summary:{collection:{state:'finished'}},
    });
    const view = newsTaskProgress(task);
    assert.equal(view.title, '원문 수집 완료 · AI 분석 대기');
    assert.equal(view.percent, 60);
    assert.equal(view.active, false);
    assert.match(view.note, /보관된 작업.*이어서 진행.*남은 자료/);
    assert.doesNotMatch(view.note, /한도|상한|집계일|20회/);
});

test('상태 조회 실패 시 기존 수치를 유지하되 지연 안내와 정지된 표시를 사용한다', () => {
    const task = running('content', {
        site_documents_processed:3, site_documents_total:8, site_name:'마지막 기관',
    });
    const view = newsTaskProgress(task, true);
    assert.equal(view.active, false);
    assert.equal(view.percent, 37);
    assert.equal(view.title, '진행 상태 확인 지연');
    assert.match(view.note, /마지막으로 확인한 값/);
    assert.equal(view.siteName, '마지막 기관');
    const html = markup(task, {unavailable:true, showNotice:false});
    assert.match(html, /마지막 확인 기관/);
    assert.match(html, /마지막으로 확인한 값/);
    assert.doesNotMatch(html, /확인 중인 기관|animate-pulse/);
});

test('이전 실행 및 미지원 스키마는 진행률을 추정하지 않고 기존 상태 안내를 유지한다', () => {
    for(const progress of [undefined, {}, {schema_version:2, phase:'analysis', analysis_processed:10, analysis_total:10}]) {
        const old = newsTaskProgress({state:'complete', progress});
        assert.equal(old.percent, null);
        assert.equal(old.active, false);
        assert.match(old.amount, /이전 실행.*상세 진행 기록이 없습니다/);
        assert.equal(old.title, '완료');
        const active = newsTaskProgress({state:'running', stage:'analyzing', progress});
        assert.equal(active.percent, null);
        assert.equal(active.active, true);
        assert.equal(active.phaseLabel, '수집 자료 분석');
    }
    assert.equal(newsTaskProgress().active, false);
});

test('보조 건수는 검증된 정수만 표시하며 기관 이름은 길이를 제한한다', () => {
    const view = newsTaskProgress(running('analysis', {
        analysis_remaining:'12', analysis_review_required:-1, analysis_failed:NaN, sites_review_required:Infinity,
    }));
    assert.deepEqual(view.lines, []);
    const collection = newsTaskProgress(running('content', {
        documents_processed:'20', attachments_processed:-1, site_name:'가'.repeat(200),
    }));
    assert.deepEqual(collection.lines, []);
    assert.equal(collection.siteName.length, 120);
});

test('알려진 진행률은 접근성 수치와 단계별 범위 설명을 제공한다', () => {
    const html = markup(running('sites', {sites_processed:12, sites_total:42, site_name:'기관 A'}));
    assert.match(html, /role="progressbar"/);
    assert.match(html, /aria-valuenow="28"/);
    assert.match(html, /aria-valuemin="0"/);
    assert.match(html, /aria-valuemax="100"/);
    assert.match(html, /aria-label="기관 게시판 확인"/);
    assert.match(html, /현재 단계 28%/);
    assert.match(html, /width:28%/);
    assert.match(html, /전체 작업 완료율·남은 시간과는 다릅니다/);
    assert.doesNotMatch(html, /animate-pulse/);
});

test('전체 분량 미확인은 퍼센트 없이 움직임 감소 설정을 지원하는 진행 표시를 사용한다', () => {
    const task = running('sites', {sites_total:null, sites_processed:0});
    const html = markup(task);
    assert.match(html, /role="progressbar"/);
    assert.match(html, /전체 분량 확인 중/);
    assert.match(html, /animate-pulse motion-reduce:animate-none/);
    assert.doesNotMatch(html, /aria-valuenow|33%<|현재 단계 33%/);
    for(const state of ['waiting','failed','complete','cancelled']) {
        assert.doesNotMatch(markup({...task, state}), /role="progressbar"|animate-pulse/);
    }
    assert.doesNotMatch(markup(task, {unavailable:true}), /role="progressbar"|animate-pulse/);
});

test('기관 이름은 HTML로 실행하지 않고 텍스트로 렌더링한다', () => {
    const html = markup(running('sites', {site_name:'<img src=x onerror=alert(1)>', sites_processed:0, sites_total:2}));
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
});

test('실행 중일 때만 2초 간격으로 확인하고 종료·대기는 10초 간격을 사용한다', () => {
    for(const state of ['ready','running']) assert.equal(newsTaskPollInterval([{state}]), 2000);
    assert.equal(newsTaskPollInterval([{state:'custom', active:true}]), 2000);
    for(const state of ['waiting','failed','complete','cancelled']) {
        assert.equal(newsTaskPollInterval([{state}]), 10000);
        assert.equal(newsTaskPollInterval([{state, active:true}]), 10000, '종료 상태가 오래된 active 플래그보다 우선');
    }
    assert.equal(newsTaskPollInterval([{state:'complete'}, {state:'running'}]), 2000);
    for(const value of [[], undefined, null, {}]) assert.equal(newsTaskPollInterval(value), 10000);
});

test('화면 내부와 전역 메뉴가 같은 진행 표시를 사용하고 조회 실패를 전달한다', async () => {
    const [layout, panel, hook] = await Promise.all([
        source('../src/components/layout/Layout.jsx'),
        source('../src/components/NewsProfileRunPanel.jsx'),
        source('../src/hooks/useNewsTaskStatus.js'),
    ]);
    assert.match(layout, /<NewsTaskProgress task=\{task\} unavailable=\{news\.isError\}/);
    assert.match(layout, /unavailable=\{news\.isError\} compact/);
    assert.doesNotMatch(layout, /새 소식에서 진행 확인/);
    assert.match(panel, /<NewsTaskProgress task=\{item\} unavailable=\{Boolean\(pollError\)/);
    assert.match(hook, /newsTaskPollInterval\(query\.state\.data\)/);
    assert.match(hook, /refetchIntervalInBackground:false/);
    assert.match(hook, /getNewsProfiles\(signal\)/);
    assert.match(hook, /getNewsWorkflows\(profile\.profile_id,signal\)/);
});

test('사이드바는 핵심 상태·현재 단계 수치·게이지만 남기고 상세는 본문에 보존한다', () => {
    const task=running('content',{site_documents_processed:3,site_documents_total:8,
        site_name:'시험 기관',documents_processed:103,attachments_processed:9});
    const html=markup(task,{compact:true});
    assert.match(html,/새 소식 · 수집 중/);
    assert.match(html,/이 기관 자료 3\/8건 · 37%/);
    assert.match(html,/aria-valuetext="이 기관 자료 3\/8건 · 현재 단계 37%"/);
    assert.doesNotMatch(html,/시험 기관|본문 확인 103건|첨부 확인 9개|전체 작업 완료율/);
    assert.match(markup(task),/시험 기관|본문 확인 103건/);
    assert.equal(compactNewsTaskStatus({...task,state:'waiting',active:false,error_code:'local_budget_exhausted'}),'이어서 진행 가능');
    assert.equal(compactNewsTaskStatus({...task,state:'waiting',active:false,error_code:'daily_quota_exhausted'}),'AI 한도 대기');
    assert.equal(compactNewsTaskStatus({...task,state:'failed',active:false}),'확인 필요');
    const stale=markup(task,{compact:true,unavailable:true});
    assert.match(stale,/새 소식 · 연결 확인 중/);
    assert.match(stale,/마지막 확인 · 이 기관 자료 3\/8건/);
    assert.doesNotMatch(stale,/animate-pulse/);
});

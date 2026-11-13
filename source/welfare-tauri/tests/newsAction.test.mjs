import assert from 'node:assert/strict';
import test from 'node:test';
import { createNewsActionGate, snapshotNewsSelection, isNewsSelectionCurrent, newsActionConfirmation } from '../src/utils/newsAction.js';

const items = [{id: 1, title: '가', input_review_required: true}, {id: 2, title: '나'}];

test('선택은 현재 목록에 남은 ID만 포함하고 경고를 중복 계산하지 않는다', () => {
    const selected = snapshotNewsSelection([{...items[0], attachment_review_required: true}, items[1]], [1, 1, 2, 999]);
    assert.deepEqual(selected.ids, [1, 2]);
    assert.equal(selected.reviewCount, 1);
});

test('확인 중 내용 변경·제외는 감지하고 목록 순서 변경은 허용한다', () => {
    const selected = snapshotNewsSelection(items, [1, 2]);
    assert.equal(isNewsSelectionCurrent(selected, items.slice().reverse()), true);
    assert.equal(isNewsSelectionCurrent(selected, [items[0]]), false);
    assert.equal(isNewsSelectionCurrent(selected, [{...items[0], input_note: '새 경고'}, items[1]]), false);
});

test('확인창은 파일 저장·Notion 등록·영구 제외·중복 재시도를 구분한다', () => {
    const selected = snapshotNewsSelection(items, [1, 2]);
    const fileConfirmation = newsActionConfirmation('html', selected);
    assert.match(fileConfirmation.title, /소식 모음 파일로 저장/);
    assert.match(fileConfirmation.confirmLabel, /파일 저장/);
    assert.doesNotMatch(JSON.stringify(fileConfirmation), /HTML/);
    assert.match(newsActionConfirmation('upload', selected).confirmLabel, /Notion 등록/);
    assert.match(newsActionConfirmation('dismiss', selected).description, /다시 표시되지 않습니다/);
    assert.match(newsActionConfirmation('retry', selected).description, /중복/);
    assert.equal(newsActionConfirmation('html', snapshotNewsSelection(items, [2])), null);
});

test('확인 대기부터 같은 프레임의 중복 실행을 막고 취소 후 다시 실행할 수 있다', async () => {
    const states = [];
    const gate = createNewsActionGate(value => states.push(value));
    let release;
    let requests = 0;
    const first = gate.run(async () => {
        if (!await new Promise(resolve => { release = resolve; })) return;
        requests++;
    });
    assert.equal(await gate.run(async () => { requests++; }), false);
    release(false);
    await first;
    assert.equal(requests, 0);
    await gate.run(async () => { requests++; });
    assert.equal(requests, 1);
    assert.deepEqual(states, [true, false, true, false]);
});

test('API 실패 시 잠금을 해제하고 다음 요청을 허용한다', async () => {
    const gate = createNewsActionGate(() => {});
    await assert.rejects(gate.run(async () => { throw Error('모의 실패'); }), /모의 실패/);
    assert.equal(await gate.run(async () => {}), true);
});

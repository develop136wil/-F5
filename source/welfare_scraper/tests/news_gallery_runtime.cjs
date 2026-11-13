// Runs the script extracted from actual Python-generated HTML, with no clipboard writes.
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function environment(mode = 'modern') {
  const calls = [], timers = new Map(), created = [], listeners = {};
  const details = [{open: false}, {open: true}, {open: false}];
  let pendingResolve, execMode = mode, sequence = 0;
  const classes = new Set();
  const notice = {textContent: '', classList: {add: x => classes.add(x), remove: x => classes.delete(x)}};
  const document = {};
  const editable = () => ({value: '', style: {}, selected: false, removed: false,
    focus() { document.activeElement = this; }, select() { this.selected = true; },
    remove() { this.removed = true; }});
  const area = editable();
  const dialog = {opened: 0, showModal() { this.opened++; }};
  const buttons = [false, true].map(disabled => ({disabled, isConnected: true,
    focus() { document.activeElement = this; }}));
  const rows = [{dataset: {copy: '첫 번째\n원문 확인 필요: 일부 누락'}}, {dataset: {copy: '두 번째 <script>문자</script>'}}];
  Object.assign(document, {
    activeElement: buttons[0],
    getElementById(id) { return {'notice': notice, 'manual-copy': dialog, 'manual-copy-text': area}[id]; },
    querySelectorAll(selector) { return selector === '.card' ? rows : selector === '.card details' ? details : buttons; },
    createElement() { const item = editable(); created.push(item); return item; },
    body: {appendChild() {}},
    execCommand(command) { calls.push(command); if (execMode === 'throw') throw Error('denied'); return execMode === 'fallback'; },
  });
  const navigator = mode === 'unavailable' ? {} : {clipboard: {writeText(text) {
    calls.push(text);
    if (mode === 'modern') return Promise.resolve();
    if (mode === 'pending') return new Promise(resolve => { pendingResolve = resolve; });
    return Promise.reject(Error('clipboard denied'));
  }}};
  const context = vm.createContext({document, navigator,
    window: {addEventListener(type, fn) { listeners[type] = fn; }},
    setTimeout(fn) { const id = ++sequence; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(script, context);
  return {context, calls, created, classes, notice, dialog, area, buttons, document, rows, timers, details, listeners,
    resolve: () => pendingResolve(), fallback: () => { execMode = 'fallback'; }};
}

test('modern clipboard success does not run fallback or open manual copy', async () => {
  const e = environment();
  await e.context.put('원문 그대로');
  assert.deepEqual(e.calls, ['원문 그대로']);
  assert.equal(e.notice.textContent, '복사했습니다.');
  assert.equal(e.dialog.opened, 0);
  assert.deepEqual(e.buttons.map(x => x.disabled), [false, true]);
});

test('permission denial can fall back only when execCommand returns true', async () => {
  const e = environment('fallback');
  await e.context.put('원문 그대로');
  assert.deepEqual(e.calls, ['원문 그대로', 'copy']);
  assert.equal(e.created[0].removed, true);
  assert.equal(e.notice.textContent, '복사했습니다.');
  assert.equal(e.dialog.opened, 0);
  assert.equal(e.document.activeElement, e.buttons[0]);
});

for (const mode of ['false', 'throw', 'unavailable']) {
  test(`${mode}: no false success, selected manual text preserves all warning/source text`, async () => {
    const e = environment(mode);
    const text = '긴 내용\n원문 확인 필요: 빠진 첨부 <script>문자</script>\nhttps://example.invalid/';
    await e.context.put(text);
    assert.equal(e.notice.textContent, '');
    assert.equal(e.classes.has('show'), false);
    assert.equal(e.dialog.opened, 1);
    assert.equal(e.area.value, text);
    assert.equal(e.area.selected, true);
    assert.equal(e.created[0].removed, true);
    assert.deepEqual(e.buttons.map(x => x.disabled), [false, true]);
  });
}

test('clipboard absent but successful legacy copy still works', async () => {
  const e = environment('unavailable'); e.fallback();
  await e.context.put('복사할 내용');
  assert.deepEqual(e.calls, ['copy']);
  assert.equal(e.notice.textContent, '복사했습니다.');
});

test('pending copy blocks duplicate invocation and restores original disabled state', async () => {
  const e = environment('pending');
  const first = e.context.put('첫 내용');
  assert.deepEqual(e.buttons.map(x => x.disabled), [true, true]);
  await e.context.put('중복 내용');
  assert.deepEqual(e.calls, ['첫 내용']);
  e.resolve(); await first;
  assert.deepEqual(e.buttons.map(x => x.disabled), [false, true]);
});

test('failure releases the gate and allows a later successful copy', async () => {
  const e = environment('false');
  await e.context.put('첫 내용'); e.fallback();
  await e.context.put('다시 복사');
  assert.equal(e.notice.textContent, '복사했습니다.');
  assert.equal(e.dialog.opened, 1);
});

test('empty input gives an honest notice without clipboard access', async () => {
  const e = environment();
  await e.context.put('  \n');
  assert.deepEqual(e.calls, []);
  assert.equal(e.notice.textContent, '복사할 내용이 없습니다.');
});

test('whole gallery uses the complete data-copy payload and separator', async () => {
  const e = environment();
  await e.context.copyAll();
  assert.deepEqual(e.calls, [e.rows.map(x => x.dataset.copy).join('\n\n──────────\n\n')]);
});

test('card copy uses only the selected card payload', async () => {
  const e = environment();
  await e.context.copyCard({closest: () => e.rows[1]});
  assert.deepEqual(e.calls, [e.rows[1].dataset.copy]);
});

test('later copy replaces the notice timer instead of hiding it too early', async () => {
  const e = environment();
  await e.context.put('하나'); await e.context.put('둘');
  assert.equal(e.timers.size, 1);
  assert.equal(e.classes.has('show'), true);
  [...e.timers.values()][0]();
  assert.equal(e.classes.has('show'), false);
});

test('printing opens every full section then restores only originally closed sections', () => {
  const e = environment();
  e.listeners.beforeprint();
  assert.deepEqual(e.details.map(x => x.open), [true, true, true]);
  e.listeners.beforeprint(); // repeated print events must not lose the original state
  e.listeners.afterprint();
  assert.deepEqual(e.details.map(x => x.open), [false, true, false]);
  e.listeners.afterprint();
  assert.deepEqual(e.details.map(x => x.open), [false, true, false]);
  assert.deepEqual(e.calls, []);
});

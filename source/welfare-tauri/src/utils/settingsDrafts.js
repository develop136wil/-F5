// In-memory only. Never persist draft secrets in browser or filesystem storage.
// next_run is server status, not a user-editable schedule setting.
const equal = (a, b) => JSON.stringify(a, (key,value) => key === 'next_run' ? undefined : value) === JSON.stringify(b, (key,value) => key === 'next_run' ? undefined : value);
export function createSettingsDraftStore() {
    const fields = new Map(), listeners = new Set();
    let version = 0, writing = false;
    const emit = () => { version++; listeners.forEach(fn => fn()); };
    return {
        subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
        getVersion: () => version,
        isWriting: () => writing,
        async runWrite(task) {
            if (writing) throw Error('다른 설정을 저장 중입니다. 완료 후 다시 저장해주세요.');
            writing = true; emit();
            try { return await task(); }
            finally { writing = false; emit(); }
        },
        ensure(name, initial) { if (!fields.has(name)) { const value = typeof initial === 'function' ? initial() : initial; fields.set(name, {value, baseline:value, revision:0}); } },
        get: name => fields.get(name)?.value,
        dirty: name => { const f = fields.get(name); return !!f && !equal(f.value, f.baseline); },
        dirtyNames() { return [...fields.keys()].filter(name => this.dirty(name)); },
        edit(name, next) { const f = fields.get(name); f.value = typeof next === 'function' ? next(f.value) : next; f.revision++; emit(); },
        capture() { return Object.fromEntries([...fields].map(([name,f]) => [name,f.revision])); },
        discard(request) {
            for (const [name,f] of fields) {
                if (request[name] !== f.revision) continue;
                f.value = f.baseline; f.revision++;
            }
            emit();
        },
        hydrate(values, request) {
            for (const [name,value] of Object.entries(values)) {
                const f = fields.get(name);
                if (!f || this.dirty(name) || (request && request[name] !== f.revision)) continue;
                f.value = value; f.baseline = value; f.revision++;
            }
            emit();
        },
        commit(name, submitted, saved = submitted) {
            const f = fields.get(name);
            if (!f) return;
            // A slow save must not discard an edit made after it started.
            if (equal(f.value, submitted)) f.value = saved;
            f.baseline = saved; f.revision++; emit();
        },
    };
}
export const settingsDrafts = createSettingsDraftStore();

// Memory only: search phrases are not written to disk or localStorage.
const initial = { inputQuery: '', query: '', category: '', page: 0, activeOnly: false, semanticMode: true };
let current = { ...initial };
export function readSearchSession() { return { ...current }; }
export function updateSearchSession(name, value) {
    if (!(name in initial)) return;
    current = { ...current, [name]: typeof value === 'function' ? value(current[name]) : value };
    return current[name];
}

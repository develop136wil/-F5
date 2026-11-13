// Keep global navigation out of native inputs during composition and modal work.
export function shouldHandleSearchShortcut(event, modalOpen = false) {
    if (modalOpen || event.defaultPrevented || event.isComposing || event.keyCode === 229) return false;
    const tag = (event.target?.tagName || '').toLowerCase();
    const typing = ['input', 'textarea', 'select'].includes(tag) || event.target?.isContentEditable;
    const isK = (event.ctrlKey || event.metaKey) && (event.key === 'k' || event.key === 'K');
    const isSlash = event.key === '/' && !typing && !event.ctrlKey && !event.metaKey && !event.altKey;
    return Boolean(isK || isSlash);
}

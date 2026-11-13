export function focusSetupStep(heading) {
    if (!heading) return;
    heading.focus({preventScroll:true});
    const dialog=heading.closest('dialog');
    if (!dialog) {heading.scrollIntoView({block:'start',behavior:'auto'});return;}
    const bottom=Math.max(dialog.getBoundingClientRect().top,
        ...Array.from(dialog.querySelectorAll('[data-setup-sticky]'),el=>el.getBoundingClientRect().bottom));
    const pane=heading.closest('[data-setup-scroll]');
    const scrollTarget=pane && getComputedStyle(pane).overflowY==='auto' ? pane : dialog;
    scrollTarget.scrollBy({top:heading.getBoundingClientRect().top-bottom-36,behavior:'auto'});
}

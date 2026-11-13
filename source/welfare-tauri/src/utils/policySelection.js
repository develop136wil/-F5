import { policyCanApply } from './policyReviewState.js';

export const fieldKey = (id, field) => `${id}::${field}`;
export const isInvalidProposal = value => typeof value !== 'string' ||
    ['', 'none', 'null', 'undefined', 'nan', '-', '(없음)'].includes(value.trim().toLowerCase());
// An unavailable proposal is not proof that the source or the stored policy is unchanged.
// Keep those fields separate from real proposals and from already-applied history.
export const groupPolicyFields = (fields = []) => {
    const groups = { priority: [], minor: [], unavailable: [], applied: [] };
    for (const field of fields) {
        if (field.applied) groups.applied.push(field);
        else if (isInvalidProposal(field.styled || field.new)) groups.unavailable.push(field);
        else if (field.level === 'low') groups.minor.push(field);
        else groups.priority.push(field);
    }
    return { ...groups, proposalCount: groups.priority.length + groups.minor.length };
};
export const isPendingPartialPolicy = policy =>
    (policy.fields || []).some(f => f.applied) && (policy.fields || []).some(f => !f.applied);
export const filterPolicyChanges = (changes, filter) => changes.filter(policy =>
    filter === 'all' || (filter === 'important'
        ? policy.intensity !== 'low' || (policy.fields || []).some(f => f.applied) || policy.apply_history?.length > 0 || ['source_review', 'needs_revalidation'].includes(policy.review_state)
        : policy.intensity === filter));
export const selectedVisibleFields = (changes, selected, includeReview = false) => changes.filter(policyCanApply).flatMap(policy =>
    (policy.fields || []).filter(field => !field.applied && (includeReview || !field.requires_individual_review) && !isInvalidProposal(field.styled || field.new) && selected.has(fieldKey(policy.id, field.field)))
        .map(field => ({ id: policy.id, field: field.field })));
export const allVisibleSelected = (changes, selected) =>
    changes.length > 0 && changes.every(policy => selected.has(policy.id));

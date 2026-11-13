import { editableSettings } from './newsProfileForm.js';

export const profileDraftKey = id => `profile:${id}`;
export function profileDraftValue(profile) {
    return {profile, draft:{...editableSettings(profile), min_age_months:profile.min_age_months ?? '', max_age_months:profile.max_age_months ?? '',
        ...Object.fromEntries(['life_cycles','characteristics','interest_topics','keywords','excluded_keywords'].map(k => [k, (profile[k] || []).join(', ')]))}};
}
export function commitProfileDraft(store, key, submitted, profile) {
    const saved = profileDraftValue(profile);
    const current = store.get(key);
    // Preserve edits made on a remounted editor, but use the newly saved revision next time.
    if (current !== submitted) store.edit(key, {...current, profile});
    store.commit(key, submitted, saved);
}

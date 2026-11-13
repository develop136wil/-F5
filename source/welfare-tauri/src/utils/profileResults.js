// Session-only outcomes and newest observed server summaries survive screen changes.
export function createProfileResults() {
    let version = 0;
    const profiles = new Map(), outcomes = new Map(), listeners = new Set();
    const emit = () => { version++; listeners.forEach(fn => fn()); };
    return {
        subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
        getVersion: () => version,
        observe(profile) {
            const previous = profiles.get(profile.profile_id);
            if (!previous || profile.revision >= previous.revision) {
                profiles.set(profile.profile_id, {...previous, ...profile}); emit();
            }
        },
        latest(profile) {
            const known = profiles.get(profile.profile_id);
            return known && known.revision >= profile.revision ? {...profile, ...known} : profile;
        },
        report(id, outcome) { outcomes.set(id, outcome); emit(); },
        outcome: id => outcomes.get(id),
    };
}
export const profileResults = createProfileResults();

import {parseTags} from './newsProfileForm.js';

export function appendKeywords(words, text) {
    const added = parseTags(text).filter(word => !words.includes(word));
    return {words: [...words, ...added], added};
}

export function restoreKeyword(words, removed) {
    if (!removed || words.includes(removed.word)) return words;
    const restored = [...words];
    restored.splice(Math.min(removed.index, restored.length), 0, removed.word);
    return restored;
}

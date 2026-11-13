export const categoriesFromSummary = (stats, selected = '') => {
    const names = Object.keys(stats?.by_category || {})
        .map(name => String(name || '').trim())
        .filter(name => name && name.toLowerCase() !== 'null' && name !== '미분류');
    if (selected && !names.includes(selected)) names.push(selected);
    return [...new Set(names)].sort((a, b) => a.localeCompare(b, 'ko'));
};

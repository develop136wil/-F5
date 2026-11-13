export function normalizeSiteIds(values) {
    if (!Array.isArray(values)) return [];
    return [...new Set(values.filter(value=>value!==null&&value!==undefined).map(value=>String(value).trim()).filter(Boolean))];
}

export function toggleProfileSite(values, siteId, selected) {
    const id=String(siteId).trim(), current=normalizeSiteIds(values);
    if(!id)return current;
    return selected ? [...current.filter(value=>value!==id),id] : current.filter(value=>value!==id);
}

export function missingProfileSiteIds(values, sites) {
    const known=new Set((Array.isArray(sites)?sites:[]).map(site=>String(site.id)));
    return normalizeSiteIds(values).filter(id=>!known.has(id));
}

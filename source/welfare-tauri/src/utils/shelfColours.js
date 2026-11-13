// Allocate distinct colours within the visible set; names remain the identifier.
const colours = [
    'bg-teal-100 border-teal-400 text-teal-900',
    'bg-sky-100 border-sky-400 text-sky-900',
    'bg-violet-100 border-violet-400 text-violet-900',
    'bg-amber-100 border-amber-400 text-amber-900',
    'bg-rose-100 border-rose-400 text-rose-900',
    'bg-lime-100 border-lime-400 text-lime-900',
    'bg-indigo-100 border-indigo-400 text-indigo-900',
    'bg-orange-100 border-orange-400 text-orange-900',
];
export function shelfColours(keys) {
    return new Map([...new Set(keys.map(String))].sort().map((key,index)=>[key,colours[index%colours.length]]));
}

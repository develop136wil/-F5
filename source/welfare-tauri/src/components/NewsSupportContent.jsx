import ReadableMarkdown from './ReadableMarkdown';

// Display only the server's readable projection. Raw extraction is not a summary.
export default function NewsSupportContent({ supportDetail, presentationKind, sourceNote }) {
    if (!supportDetail && !sourceNote) return null;
    // New projections introduce this fixed heading themselves; the section title
    // already supplies it. Preserve every source sentence and legacy Markdown.
    const content = ['structured', 'source_excerpt'].includes(presentationKind)
        ? supportDetail?.replace(/^### 지원 내용\r?\n/, '') : supportDetail;
    return <section className="p-5" aria-label="지원 내용">
        <h3 className="mb-2 text-sm font-bold text-gray-700 dark:text-gray-300">지원 내용</h3>
        {presentationKind === 'source_excerpt' && supportDetail && !sourceNote &&
            <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">원문에서 확인한 내용입니다.</p>}
        {content && <ReadableMarkdown text={content}/>}
        {sourceNote && <p className="mt-3 break-words text-xs leading-5 text-gray-500 dark:text-gray-400">{sourceNote}</p>}
    </section>;
}

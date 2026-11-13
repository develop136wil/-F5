import {storedNewsPresentation} from './newsResultDetails.js';
import {markdownBlocks, markdownInline} from './readableMarkdown.js';

export function searchPreviewText(item) {
    // Policy prose is not a news extraction and retains its existing display.
    let ai = item?.ai_data;
    if (typeof ai === 'string') {try {ai=JSON.parse(ai);} catch {ai=null;}}
    if (item?.item_type === 'policy' || ai?.item_type === 'policy') {
        return item.support_detail || item.target || '제공된 상세 정보가 없습니다.';
    }
    const display = storedNewsPresentation(item);
    // Missing, invalid or explicitly empty projections never reveal canonical
    // extraction text. The detail dialog and source link remain available.
    if (!display?.support_detail) {
        return display?.source_note || '내용은 상세 정보 또는 원문에서 확인해주세요.';
    }
    return markdownBlocks(display.support_detail).flatMap(block => {
        if (block.type === 'rule') return [];
        const lines = block.type === 'list' ? block.items : [block.text];
        return lines.map(line => block.type === 'code' ? line
            : markdownInline(line).map(part => part.text).join(''));
    }).join(' ').replace(/\s+/g, ' ').trim() || display.source_note
        || '내용은 상세 정보 또는 원문에서 확인해주세요.';
}

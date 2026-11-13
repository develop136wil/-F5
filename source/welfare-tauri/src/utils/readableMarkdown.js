// A deliberately small, text-only Markdown subset. Never interpret raw HTML.
export function markdownBlocks(value) {
    const lines=String(value ?? '').replace(/\r\n?/g,'\n').replace(/<br\s*\/?\s*>/gi,'\n').split('\n');
    const blocks=[];
    let paragraph=[], list=null, code=null;
    const flush=()=>{if(paragraph.length){blocks.push({type:'paragraph',text:paragraph.join('\n')});paragraph=[];}list=null;};
    for(const line of lines){
        if(/^\s*```/.test(line)) {flush();if(code!==null){blocks.push({type:'code',text:code.join('\n')});code=null;}else code=[];continue;}
        if(code!==null){code.push(line);continue;}
        if(!line.trim()){flush();continue;}
        const heading=line.match(/^\s{0,3}#{1,6}[ \t]+(.+)$/);
        if(heading){flush();blocks.push({type:'heading',text:heading[1].replace(/[ \t]+#+[ \t]*$/,'').trimEnd()});continue;}
        const bullet=line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
        if(bullet){
            const ordered=/^\d/.test(bullet[1]);
            if(!list||list.ordered!==ordered){flush();list={type:'list',ordered,start:ordered?Number.parseInt(bullet[1],10):undefined,items:[]};blocks.push(list);}
            list.items.push(bullet[2]);continue;
        }
        if(list&&/^\s{2,}\S/.test(line)){list.items[list.items.length-1]+='\n'+line.trim();continue;}
        if(list)flush();
        if(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)){flush();blocks.push({type:'rule'});continue;}
        paragraph.push(line);
    }
    flush();
    if(code!==null)blocks.push({type:'code',text:code.join('\n')});
    return blocks;
}

export function markdownInline(value) {
    const text=String(value), parts=[];
    const pattern=/\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let cursor=0;
    for(const match of text.matchAll(pattern)){
        if(match.index>cursor)parts.push({type:'text',text:text.slice(cursor,match.index)});
        if(match[1]||match[2])parts.push({type:'strong',text:match[1]||match[2]});
        else if(match[3])parts.push({type:'code',text:match[3]});
        else parts.push({type:'link',text:match[4],href:match[5]});
        cursor=match.index+match[0].length;
    }
    if(cursor<text.length)parts.push({type:'text',text:text.slice(cursor)});
    return parts;
}

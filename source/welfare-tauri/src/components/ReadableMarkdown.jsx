import React from 'react';
import {markdownBlocks, markdownInline} from '../utils/readableMarkdown';

function Inline({text}) {
    return markdownInline(text).map((part,index)=>part.type==='strong'?<strong key={index}>{part.text}</strong>
        :part.type==='code'?<code key={index} className="rounded bg-gray-100 px-1 dark:bg-gray-800">{part.text}</code>
        :part.type==='link'?<a key={index} href={part.href} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline dark:text-blue-400">{part.text}</a>
        :<React.Fragment key={index}>{part.text}</React.Fragment>);
}

export default function ReadableMarkdown({text}) {
    return <div className="space-y-3 break-words text-sm leading-relaxed text-gray-600 dark:text-gray-400 [overflow-wrap:anywhere]">
        {markdownBlocks(text).map((block,index)=>{
            if(block.type==='heading')return <h4 key={index} className="font-semibold text-gray-700 dark:text-gray-300"><Inline text={block.text}/></h4>;
            if(block.type==='list'){
                const Tag=block.ordered?'ol':'ul';
                return <Tag key={index} start={block.ordered?block.start:undefined} className={`${block.ordered?'list-decimal':'list-disc'} space-y-1 pl-5`}>{block.items.map((item,itemIndex)=><li key={itemIndex} className="whitespace-pre-wrap"><Inline text={item}/></li>)}</Tag>;
            }
            if(block.type==='code')return <pre key={index} className="whitespace-pre-wrap rounded bg-gray-50 p-2 dark:bg-gray-800">{block.text}</pre>;
            if(block.type==='rule')return <hr key={index} className="border-gray-100 dark:border-gray-700"/>;
            return <p key={index} className="whitespace-pre-wrap"><Inline text={block.text}/></p>;
        })}
    </div>;
}

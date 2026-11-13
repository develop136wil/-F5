import React, {useState} from 'react';

const SOURCE = 'https://videos.ctfassets.net/spoqsaf9291f/6xAdDunrpdVviEtLz4UZ0A/93448609bf9425ef73be1e4ede80aab7/Duplicate_public_page.mp4';
const HELP = 'https://www.notion.com/ko/help/duplicate-public-pages';

// Mounted only in an open duplication step. Show the player, never autoplay.
export default function NotionDuplicateVideo() {
    const [open, setOpen] = useState(true);
    const [failed, setFailed] = useState(false);
    return <div data-notion-video className="mt-4 max-w-xl text-sm">
        <div className="flex items-center justify-between gap-3">
            <p className="font-medium text-gray-700 dark:text-gray-200">Notion 공식 복제 영상</p>
            <button type="button" aria-expanded={open} onClick={() => {setFailed(false);setOpen(value => !value);}}
                className="min-h-9 rounded-lg px-2 text-sm text-gray-600 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-300 dark:hover:bg-gray-800">
                {open ? '영상 접기' : '영상 펼치기'}
            </button>
        </div>
        {open && <figure className="mt-2">
            {!failed && <video src={SOURCE} controls playsInline preload="metadata" aria-label="Notion 공개 페이지 복제 예시"
                onError={() => setFailed(true)} className="aspect-video max-h-64 w-full rounded-xl bg-gray-950 object-contain">
                브라우저에서 영상을 지원하지 않습니다. 아래 공식 안내를 확인해주세요.
            </video>}
            {failed && <div className="rounded-xl border border-gray-200 p-4 dark:border-gray-700"><p role="status" className="text-gray-600 dark:text-gray-300">영상을 불러오지 못했습니다. 위의 설명으로 진행하거나 공식 안내를 열어주세요.</p><button type="button" onClick={()=>setFailed(false)} className="mt-2 min-h-9 rounded-lg px-2 text-blue-700 underline focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300">영상 다시 불러오기</button></div>}
            <figcaption className="mt-2 text-xs leading-5 text-gray-600 dark:text-gray-300">
                로그인 → 복제 → 사용할 작업 공간 선택 순서입니다. 예시 영상은 실제 화면 언어와 다를 수 있습니다.
                {' '}<a href={HELP} target="_blank" rel="noopener noreferrer" className="underline">Notion 공식 안내 ↗</a>
            </figcaption>
        </figure>}
    </div>;
}

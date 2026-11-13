import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, RefreshCcwDot, Newspaper, Search, BarChart3, Globe2 } from 'lucide-react';

function WorkflowCard({ title, description, status, to, action, Icon, news = false }) {
    return <article className={`relative isolate flex min-h-[304px] min-w-0 flex-col overflow-hidden rounded-3xl border p-6 shadow-sm lg:min-h-[328px] lg:p-8 xl:min-h-[352px] ${news
        ? 'border-teal-100 bg-gradient-to-br from-teal-50 to-white dark:border-teal-900 dark:from-teal-950 dark:to-gray-900'
        : 'border-blue-100 bg-gradient-to-br from-blue-50 to-white dark:border-blue-900 dark:from-blue-950 dark:to-gray-900'}`}>
        <Icon aria-hidden="true" strokeWidth={1.2} className={`pointer-events-none absolute -right-5 -top-5 -z-10 h-48 w-48 rotate-[-12deg] opacity-[0.07] lg:h-56 lg:w-56 ${news ? 'text-teal-700 dark:text-teal-200' : 'text-blue-700 dark:text-blue-200'}`} />
        <h2 className="text-2xl font-bold leading-snug text-gray-900 dark:text-white xl:text-[1.75rem] [word-break:keep-all]">{title}</h2>
        <p className="mb-6 mt-4 max-w-xl text-sm leading-6 text-gray-600 dark:text-gray-300 [word-break:keep-all]">{description}</p>
        <div className="mt-auto border-t border-gray-200/70 pt-4 dark:border-gray-700">
            <p role="status" className={`mb-4 text-sm font-semibold ${news ? 'text-teal-800 dark:text-teal-200' : 'text-blue-800 dark:text-blue-200'}`}>{status}</p>
            <Link to={to} className={`inline-flex min-h-[72px] w-full items-center justify-center gap-2 rounded-xl px-4 py-4 text-center text-base font-semibold text-white transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 dark:ring-offset-gray-900 ${news
                ? 'bg-teal-700 hover:bg-teal-800 focus-visible:ring-teal-600' : 'bg-blue-600 hover:bg-blue-700 focus-visible:ring-blue-500'}`}>
                <span className="min-w-0 [word-break:keep-all] [overflow-wrap:anywhere]">{action}</span><ArrowRight aria-hidden="true" className="h-4 w-4 shrink-0" />
            </Link>
        </div>
    </article>;
}

export default function HomeWorkspace({ policyCount = 0, newsCount = 0, policyStatus, newsStatus, isScraping = false, progress = 0 }) {
    return <div className="space-y-5">
        <section aria-label="주요 업무" className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,18rem),1fr))] gap-4 lg:gap-5">
            <WorkflowCard to="/policy" title="정책 최신화" Icon={RefreshCcwDot}
                description="등록된 정책이 바뀌었는지 확인하고, 검토한 내용을 Notion에 반영합니다."
                status={policyStatus ?? `검토 대기 ${policyCount}건`} action="정책 변경 확인하기" />
            <WorkflowCard to="/notion" title="새 소식" Icon={Newspaper} news
                description="기관 홈페이지에서 새 게시물을 찾고, 필요한 소식을 골라 저장합니다."
                status={isScraping ? `찾는 중 · ${progress}%` : (newsStatus ?? `등록 후보 ${newsCount}건`)}
                action={isScraping ? '새 소식 진행 상황 보기' : '새 소식 탐색하기'} />
        </section>
        <nav aria-label="바로가기" className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,10rem),1fr))] gap-3">
            {[[Search, '/search', '통합 검색', '저장된 복지 정보를 한곳에서 찾습니다.'],
              [BarChart3, '/reports', '리포트·내보내기', '수집 결과를 확인하고 파일로 저장합니다.'],
              [Globe2, '/sites', '수집 사이트 등록·관리', '새 소식을 찾을 기관 홈페이지를 추가합니다.']].map(([Icon, to, title, description]) =>
                <Link key={to} to={to} className="relative isolate overflow-hidden flex min-h-[104px] min-w-0 flex-col items-center justify-center rounded-2xl border border-gray-100 bg-white px-4 py-4 text-center text-gray-700 transition-colors hover:border-blue-200 hover:bg-blue-50/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-700 dark:bg-[#1e2128] dark:text-gray-200 dark:hover:bg-gray-800">
                    <Icon aria-hidden="true" strokeWidth={1.2} className="pointer-events-none absolute -right-3 -top-3 -z-10 h-24 w-24 rotate-[-12deg] text-blue-700 opacity-[0.07] xl:h-28 xl:w-28 dark:text-blue-200" />
                    <span className="text-sm font-bold [word-break:keep-all]">{title}</span>
                    <span className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400 [word-break:keep-all]">{description}</span>
                </Link>)}
        </nav>
    </div>;
}

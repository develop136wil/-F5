import React from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card';
import { Globe, Search, Settings, Zap, Brain, RefreshCcwDot, UploadCloud } from 'lucide-react';
import { motion } from 'framer-motion';
import SetupDialog from '../components/onboarding/SetupDialog';
import SetupFeatureCards from '../components/onboarding/SetupFeatureCards';
import { Link, useLocation } from 'react-router-dom';

const steps = [
    {
        icon: Settings,
        title: 'AI 연결·수집 조건',
        color: 'text-purple-500',
        bg: 'bg-purple-50 dark:bg-purple-900/20',
        content: [
            '기관 사이트에서 새 소식을 찾으려면 설정 도우미의 "새 소식 탐색 준비"에서 Gemini API 키를 연결하세요.',
            '지역 → 생애주기·가구상황 → 나이 → 최종 저장 순서로 기본 조건을 만드세요. "새 소식 찾기"와 예약 수집에 적용됩니다.'
        ]
    },
    {
        icon: Globe,
        title: '사이트 등록',
        color: 'text-blue-500',
        bg: 'bg-blue-50 dark:bg-blue-900/20',
        content: [
            '홈 → "수집 사이트 등록·관리"에서 기관의 게시판 주소를 입력하세요.',
            '"주소 등록"을 누르면 수집 규칙을 확인합니다. 대표 홈페이지 대신 공지사항·모집 게시글 목록 화면의 주소를 입력하세요.'
        ]
    },
    {
        icon: Zap,
        title: '새 소식 찾기',
        color: 'text-yellow-500',
        bg: 'bg-yellow-50 dark:bg-yellow-900/20',
        content: [
            '"새 소식 찾기"를 누르고 화면의 조건과 확인할 곳을 확인한 뒤 시작하세요.',
            '기본 조건은 등록된 모든 기관 사이트를, 다른 조건은 그 조건에 선택한 사이트·정보 출처를 확인합니다.',
            '관련 결과는 "찾은 소식" 하나에 모입니다. 미분석 자료는 "확인 필요", 제외된 자료는 접힌 처리 기록에서 확인합니다.',
            '기본 게시 기간은 최근 30일이며 수집 조건에서 7일·90일·직접 지정으로 바꿀 수 있습니다. 수집일·마감일이 아닌 게시일 기준이고, 기존 자료를 삭제하지 않습니다.'
        ]
    },
    {
        icon: RefreshCcwDot,
        title: '정책 최신화',
        color: 'text-indigo-500',
        bg: 'bg-indigo-50 dark:bg-indigo-900/20',
        content: [
            '설정 도우미의 "정책 최신화 준비"에서 AI와 Notion 정책표를 연결하세요. 새 소식 수집과는 별도 기능입니다.',
            '"정책 정보 최신인지 확인하기"로 연결된 정책표와 출처의 변경을 점검합니다.',
            '제안을 검토한 뒤 속성별로 "바로 적용"하거나 선택한 내용을 한 번에 Notion에 반영하세요. 자동 반영하지 않습니다.',
            '새 정책 URL만 있다면 "정책 등록 후보" 탭의 "직접 찾은 정책으로 초안 만들기"에서 내용을 확인·수정한 뒤 Notion에 추가하세요.',
            '"웹페이지 변경 확인"에서 개별 페이지도 별도 등록 가능'
        ]
    },
    {
        icon: UploadCloud,
        title: '새 소식 저장',
        color: 'text-emerald-500',
        bg: 'bg-emerald-50 dark:bg-emerald-900/20',
        content: [
            '제목을 눌러 대상과 지원 내용을 상세 창에서 확인하세요. AI 분석은 지원 자격의 확정 판정이 아닙니다.',
            '"찾은 소식" 목록에서 필요한 항목만 선택해 소식 모음 파일로 저장하거나 Notion에 등록하세요.',
            '소식 모음 파일 저장은 Notion 없이 가능. Notion 등록은 설정 도우미의 "Notion 보관함 연결"을 먼저 완료하세요.'
        ]
    },
    {
        icon: Search,
        title: '통합 검색·리포트',
        color: 'text-green-500',
        bg: 'bg-green-50 dark:bg-green-900/20',
        content: [
            '"통합 검색"에서 저장된 자료를 키워드·의미 검색과 카테고리 필터로 찾으세요.',
            '홈의 "리포트·내보내기"에서 CSV·Excel 파일이나 브라우저로 열어보는 리포트를 만드세요.'
        ]
    },
    {
        icon: Brain,
        title: '설정 관리',
        color: 'text-gray-500',
        bg: 'bg-gray-50 dark:bg-gray-800/40',
        content: [
            '현재 수집 대상은 설정 → "수집 설정" → "새 소식 수집 조건"에서 바로 바꾸세요. 전에 저장한 별도 조건과 분석 기록은 "저장한 다른 조건"에서 확인합니다.',
            '찾을 단어·제외할 단어·AI 요청은 각 조건의 "수집 세부 설정"에서 바꿉니다. 보관한 다른 조건은 새 소식의 "다른 조건 선택"에서 따로 실행하며, 현재 조건과 예약은 바뀌지 않습니다.',
            '설정 → "자동화/알림"에서 수집 예약과 알림 연결을 관리하세요.',
            '설정 → "시스템 관리"에서 점검 상태·로그·백업/복원을 확인하세요. 자주 쓰지 않는 기능은 "고급 시스템 관리"와 "고급 도구"에 있습니다.'
        ]
    }
];

const container = {
    hidden: { opacity: 0 },
    show: {
        opacity: 1,
        transition: { staggerChildren: 0.1 }
    }
};

const itemVariant = {
    hidden: { opacity: 0, y: 20 },
    show: { opacity: 1, y: 0 }
};

const Tutorial = ({ embedded = false }) => {
    const location = useLocation();
    const step = new URLSearchParams(location.search).get('step');
    const lastStep = window.localStorage.getItem('welfare-f5-setup-resume');
    const resumeStep = ['setup', 'google', 'target', 'sites', 'policy', 'news'].includes(lastStep) ? lastStep : null;
    const resumeLabel = {setup:'처음 설정',google:'AI 연결',target:'새 소식 대상 설정',sites:'수집 사이트 설정',policy:'정책 최신화 준비',news:'새 소식 탐색 준비'}[resumeStep];
    return (
        <div className="space-y-6 max-w-4xl mx-auto">
            {!embedded && <div className="text-center space-y-2 mb-8">
                <h1 className="ui-page-title">도움말</h1>
                <p className="text-gray-500">먼저 사용할 기능을 고르세요. 처음 준비할 것부터 연결 완료까지 순서대로 안내합니다.</p>
                <p className="text-xs text-gray-400 dark:text-gray-500">
                    복지F5 — 복지정보를 새로고침하다 (Find · Filter · Fact-check · Follow-up · Fresh)
                </p>
            </div>}

            <section className="rounded-2xl border border-blue-100 bg-white p-5 space-y-3 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100">
                <h2 className="text-xl font-semibold leading-snug">설정 도우미</h2>
                <p className="text-sm text-gray-600 dark:text-gray-300">설정할 기능을 선택하세요.</p>
                <SetupFeatureCards active={!step} />
                {resumeStep && <Link to={`/settings?tab=guide&step=${resumeStep}`} className="inline-block text-sm text-gray-600 underline underline-offset-4">최근 열었던 안내: {resumeLabel}</Link>}
            </section>
            {step && <SetupDialog />}

            <details className="group border-t border-gray-200 dark:border-gray-700">
                <summary className="cursor-pointer list-none px-5 py-4 flex items-center justify-between text-sm font-semibold text-gray-700 dark:text-gray-200 hover:bg-white dark:hover:bg-gray-800 transition-colors">
                    기능별 사용 방법
                    <span className="text-xs text-gray-400 group-open:hidden">필요할 때 펼쳐보기</span>
                    <span className="text-xs text-gray-400 hidden group-open:inline">접기</span>
                </summary>
                <motion.div
                    className="divide-y divide-gray-200 px-4 dark:divide-gray-700"
                    variants={container}
                    initial="hidden"
                    animate="show"
                >
                {steps.map((step, idx) => (
                    <motion.div key={idx} variants={itemVariant}>
                        <section className="py-4">
                            <header className="pb-3">
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100">
                                    {step.title}
                                </h3>
                            </header>
                            <div>
                                <ul className="space-y-2">
                                    {step.content.map((line, i) => (
                                        <li key={i} className="text-sm text-gray-600 dark:text-gray-400 flex items-start gap-2">
                                            <span className="text-gray-300 mt-1">•</span>
                                            {line}
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        </section>
                    </motion.div>
                ))}
                </motion.div>
            </details>

        </div>
    );
};

export default Tutorial;

import React, { useLayoutEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, ChevronDown } from 'lucide-react';
import SetupManualLink from './SetupManualLink';
import { GOOGLE_AI_STUDIO_API_KEY_URL, NOTION_INTEGRATIONS_URL } from '../../constants/onboardingLinks';
import ExternalActionLink from './ExternalActionLink';
import {focusSetupStep} from '../../utils/focusSetupStep';

const GOOGLE_STEPS = [
    {
        title: 'Google AI Studio에서 API 키 확인하기',
        image: '/guide-images/google-api-list.png',
        imageAlt: 'Google AI Studio의 API 키 목록과 API 키 만들기 버튼',
        imageCaption: '목록에 키가 있으면 기존 키를 사용할 수 있습니다. 사진의 프로젝트 이름은 예시입니다.',
        instruction: '아래 링크로 Google 계정에 로그인하세요. 처음 접속하면 이용약관 화면이 나올 수 있습니다. 내용을 확인한 뒤 동의하고 계속하면 API 키 목록으로 이동할 수 있습니다.',
        screen: 'Google AI Studio',
        path: 'Dashboard  ›  API Keys',
        action: 'API Keys',
        finish: '키 목록이 보이면 다음으로 이동하세요.',
        trouble: '메뉴를 찾기 어렵다면 이 안내의 Google AI Studio 링크로 API 키 목록을 직접 여세요. 로그인 화면이 나오면 먼저 Google 계정으로 로그인하세요.',
        href: GOOGLE_AI_STUDIO_API_KEY_URL, linkLabel: 'Google AI Studio 열기',
    },
    {
        title: '기존 키 확인 또는 새 키 만들기',
        instruction: '목록에 키가 있으면 해당 키를 선택하세요. 없다면 API 키 만들기(Create API key)를 누릅니다. 새 사용자는 기본 프로젝트와 키가 이미 만들어져 있을 수도 있습니다.',
        screen: 'API Keys',
        path: '내 API 키',
        action: '＋ Create API key',
        finish: '새 키가 목록에 나타나면 다음으로 이동하세요.',
        trouble: '이미 키가 있다면 새로 만들지 않고 기존 키의 복사 버튼을 사용해도 됩니다.',
    },
    {
        title: '표시된 키 전체 복사',
        instruction: '키를 선택한 뒤 복사(Copy)를 눌러 키 전체를 복사하세요. API 키는 비밀번호처럼 관리하고 다른 사람에게 공유하거나 화면 캡처에 남기지 마세요.',
        screen: 'API key details',
        path: '복지F5용 Gemini 키',
        secret: '••••••••••••••••••••••••••',
        action: 'Copy',
        finish: '키는 채팅·문서에 붙이지 말고 복지F5로 바로 돌아오세요.',
        trouble: '키 값은 화면 캡처나 메모장에 남기지 마세요. 복사 버튼만 누르면 됩니다.',
    },
    {
        title: '복지F5에 붙여넣고 확인',
        instruction: '이 안내 아래의 Gemini API 키 칸에 붙여넣고 저장하고 연결 확인을 누르세요. AI 연결 완료 표시를 확인한 뒤 다음 단계로 진행합니다. 다른 설정 화면으로 이동하지 않아도 됩니다.',
        screen: '현재 설정 도우미의 입력칸',
        path: 'Google Gemini API 키',
        secret: '붙여넣은 키는 화면에서 숨겨집니다',
        action: '저장하고 연결 확인',
        finish: 'AI 연결 완료 안내가 표시되면 다음 단계로 진행하세요.',
        appScreen: true,
        trouble: '연결에 실패하면 키 앞뒤에 빈칸이 들어갔는지 먼저 확인하세요.',
    },
];

const NOTION_SIGNUP_STEPS = [
    { title: 'Notion 가입·로그인', instruction: '계정이 없다면 Notion에 가입하세요. 이미 계정이 있다면 새로 가입하지 않고 로그인하세요.', href: 'https://www.notion.com/ko', linkLabel: 'Notion 가입·로그인 열기' },
    { title: '기본 정보와 사용 목적 선택', instruction: '처음 가입하는 경우 화면에 따라 이름과 사용 목적을 입력하세요. 팀원 초대와 데스크톱 앱 설치는 필요한 경우에만 진행합니다.' },
    { title: '요금제 선택과 앱 설치 확인', instruction: '매뉴얼은 무료 요금제를 선택하는 예시입니다. 화면의 이용 조건을 확인하고 사용할 요금제를 선택하세요. 데스크톱 앱 설치는 건너뛰고 브라우저에서 계속할 수 있습니다.' },
    { title: '사용할 작업 공간 확인', instruction: '정책표나 새 소식 보관함을 보관할 작업 공간(워크스페이스)을 확인하세요. 다음 API 연결과 템플릿 복제에서도 같은 작업 공간을 선택합니다.' },
];

const NOTION_STEPS = [
    {
        title: 'Notion 가입·로그인',
        instruction: 'Notion 계정이 없다면 가입하고 이름과 사용 목적을 선택하세요. 매뉴얼은 무료 요금제 선택 예시이며, 화면의 이용 조건을 확인해 선택합니다. 팀원 초대와 데스크톱 앱 설치는 필요한 경우에만 진행하세요. 정책표나 보관함을 보관할 작업 공간(워크스페이스)을 정하세요. 이미 사용 중이라면 다음으로 진행해도 됩니다.',
        screen: 'Notion', path: '로그인 또는 가입', action: '로그인 / 무료로 시작하기',
        href: 'https://www.notion.com/ko', linkLabel: 'Notion 가입·로그인 열기',
        finish: '내 작업 공간에 들어왔다면 다음으로 진행하세요.',
        trouble: '기관의 작업 공간을 사용한다면 소유자에게 연결 생성 권한을 확인하세요.',
    },
    {
        title: '내부 연결 만들기',
        image: '/guide-images/notion-create.png',
        imageAlt: 'Notion 내부 API 통합의 새 API 통합 만들기 버튼',
        imageCaption: '파란 테두리 안의 새 API 통합 만들기를 선택하세요. 화면 언어에 따라 영어로 표시될 수 있습니다.',
        href: NOTION_INTEGRATIONS_URL, linkLabel: 'Notion 연결 관리 열기',
        instruction: 'Notion 연결 관리 화면에서 신규 연결(Create a new connection)을 누르세요. 인증 방식 선택 화면이 나오면 액세스 토큰을 선택합니다. 기존 화면에서는 내부 API 통합으로 표시될 수 있습니다.',
        screen: 'Notion Developer portal',
        path: 'Build  ›  Internal connections',
        action: 'Create a new connection',
        finish: '이름은 ‘복지F5’처럼 알아보기 쉽게 정해도 됩니다.',
        trouble: '이 메뉴가 없으면 현재 계정이 해당 워크스페이스의 소유자인지 확인하세요.',
    },
    {
        title: '이름과 작업 공간 선택',
        instruction: '연결 이름은 복지F5처럼 알아보기 쉽게 입력하세요. 정책표나 새 소식 보관함을 복제할 작업 공간을 선택한 뒤 연결 생성하기를 누르세요.',
        screen: '새 API 통합', path: '기본 정보', action: '생성하기',
        image: '/guide-images/notion-workspace.png',
        imageAlt: 'Notion API 통합 이름과 관련 워크스페이스 입력 위치',
        imageCaption: '사진의 투두투는 예시입니다. 본인의 작업 공간을 선택하세요.',
        finish: '생성 완료 창이 나오면 API 통합 설정 구성을 눌러 다음으로 진행하세요.',
        trouble: '작업 공간이 없다면 Notion에 먼저 로그인하고 사용할 작업 공간을 확인하세요.',
    },
    {
        title: '권한 확인 후 토큰 복사',
        instruction: '구성(Configuration)에서 콘텐츠 읽기·수정·추가 권한을 확인하고 액세스 토큰 또는 프라이빗 API 통합 시크릿(연결 토큰)을 복사합니다. 페이지 접근 권한은 다음 표 연결 단계에서 설정합니다.',
        screen: 'Connection  ›  Configuration',
        path: 'Capabilities',
        permissions: ['Read content', 'Update content', 'Insert content'],
        secret: 'Installation access token',
        action: 'Copy',
        finish: '복사한 토큰은 복지F5의 Notion 연결 토큰 칸에만 입력하세요.',
        trouble: '토큰이 보이지 않으면 만든 연결의 Configuration 화면인지 확인하세요.',
    },
    {
        title: '복지F5에 토큰 붙여넣기',
        instruction: '이 안내 아래의 Notion 연결 토큰 칸에 복사한 액세스 토큰을 붙여넣고 연결 토큰 저장을 누르세요. 토큰 저장만으로는 정책표나 보관함에 접근할 수 없습니다. 다음 단계에서 템플릿을 복제한 뒤, 복제한 페이지에 이 연결의 접근 권한을 추가합니다.',
        screen: '현재 설정 도우미의 입력칸',
        path: 'Notion 연결 토큰',
        secret: '토큰은 화면에서 숨겨집니다',
        action: '연결 토큰 저장',
        finish: '토큰 저장 안내가 표시되면 다음 단계에서 사용할 표를 연결하세요.',
        appScreen: true,
        trouble: '토큰 저장만으로는 표의 접근 권한이 생기지 않습니다. 다음 단계도 이어서 진행하세요.',
    },
];

const NEWS_GALLERY_STEPS = [
    {
        title: '내 Notion에 보관함 만들기',
        instruction: 'Notion에 로그인한 뒤 복지F5 새 소식 보관함 템플릿을 여세요. 오른쪽 위의 Duplicate(복제)를 누르고, 앞에서 연결을 만든 작업 공간을 선택하세요.',
        screen: 'Notion  ›  복지F5 새 소식 보관함',
        path: '오른쪽 위',
        action: '겹친 네모 모양 · Duplicate (복제)',
        finish: '내 Notion에 새 소식 보관함이 생기면 다음으로 이동하세요. 예시 카드는 나중에 지워도 됩니다.',
        trouble: '복제 버튼이 보이지 않으면 Notion에 로그인했는지와 템플릿이 새 창에서 열렸는지 확인하세요.',
    },
    {
        title: '복제한 보관함에 연결 권한 추가하기',
        instruction: '내 작업 공간에 복제한 보관함에서 오른쪽 위 ••• → 연결(Connections)을 여세요. 연결 목록이나 연결 추가(Add connections)에서 앞서 만든 연결을 선택합니다. 이름을 복지F5로 정했다면 복지F5를 찾으세요. 권한을 확인한 뒤 페이지에 추가를 눌러 마칩니다. 템플릿 원본이 아닌 내 보관함에서 진행하세요.',
        screen: '내 Notion의 새 소식 보관함',
        path: '•••  ›  Connections (연결)',
        action: '만든 연결 선택 → 페이지에 추가',
        finish: '목록에 복지F5 연결이 표시되면 권한 설정이 끝난 것입니다.',
        trouble: '복지F5 연결이 검색되지 않으면 보관함과 내부 연결을 같은 워크스페이스에서 만들었는지 확인하세요.',
    },
    {
        title: '보관함 주소 복사',
        instruction: '템플릿 원본이 아닌, 내 작업 공간에 복제한 보관함을 여세요. 카드 하나가 아니라 보관함 전체 페이지에서 오른쪽 위 공유(Share)를 누르고 링크 복사(Copy link)를 선택하세요.',
        screen: '내 Notion의 새 소식 보관함',
        path: '오른쪽 위 공유(Share) → 링크 복사(Copy link)',
        action: '링크 복사 (Copy link)',
        copyLink: true,
        finish: '표 안의 카드가 아니라 보관함 전체 화면의 주소를 복사하세요.',
        trouble: '주소를 복사하기 전에 왼쪽 위에 보관함 제목이 표시되는 전체 화면인지 확인하세요.',
    },
    {
        title: '복지F5에 붙여넣고 확인',
        instruction: '복지F5로 돌아와 아래의 내 새 소식 보관함 주소 칸을 클릭하세요. Ctrl+V 또는 마우스 오른쪽 버튼 → 붙여넣기로 복사한 주소를 넣은 뒤 저장하고 보관함 연결 확인을 누르세요.',
        screen: '현재 설정 도우미의 입력칸',
        path: '내 새 소식 보관함 주소',
        secret: 'https://www.notion.so/내-새소식-보관함-…',
        action: '저장하고 보관함 연결 확인',
        finish: '새 소식 보관함 연결 완료 안내가 표시되면 완료입니다.',
        appScreen: true,
        trouble: '확인에 실패하면 연결 권한과 이름·기관·선택·기간 네 항목의 이름과 종류를 확인하세요.',
    },
];

const POLICY_STEPS = [
    {
        title: '내 Notion에 정책표 만들기',
        instruction: 'Notion에 로그인한 뒤 복지F5 정책표 템플릿을 여세요. 오른쪽 위의 Duplicate(복제)를 누르고, 앞에서 연결을 만든 작업 공간을 선택하세요. 원본이 아닌 내 정책표가 만들어집니다.',
        screen: 'Notion  ›  복지F5 정책',
        path: '오른쪽 위',
        action: 'Duplicate (복제)',
        finish: '내 Notion에 ‘복지F5 정책’ 표가 생기면 다음으로 이동하세요. 원본과는 별개의 내 표입니다.',
        trouble: 'Duplicate가 보이지 않으면 Notion에 로그인했는지와 템플릿이 새 창에서 열렸는지 확인하세요.',
    },
    {
        title: '복제한 정책표에 연결 권한 추가하기',
        instruction: '내 작업 공간에 복제한 정책표에서 오른쪽 위 ••• → 연결(Connections)을 여세요. 연결 목록이나 연결 추가(Add connections)에서 앞서 만든 연결을 선택합니다. 이름을 복지F5로 정했다면 복지F5를 찾으세요. 권한을 확인한 뒤 페이지에 추가를 눌러 마칩니다. 템플릿 원본이 아닌 내 정책표에서 진행하세요.',
        screen: '내 Notion의 복지F5 정책표',
        path: '•••  ›  Connections',
        action: '만든 연결 선택 → 페이지에 추가',
        finish: '정책표가 여러 개라면 사용할 표마다 한 번씩 권한을 주세요.',
        trouble: '복지F5 연결이 검색되지 않으면 정책표와 내부 연결을 같은 워크스페이스에서 만들었는지 확인하세요.',
    },
    {
        title: '정책표 주소 복사',
        instruction: '템플릿 원본이 아닌, 내 작업 공간에 복제한 정책표를 여세요. 표 안의 한 행이 아니라 정책표 전체 페이지에서 오른쪽 위 공유(Share)를 누르고 링크 복사(Copy link)를 선택하세요.',
        screen: '내 Notion의 복지F5 정책표',
        path: '오른쪽 위 공유(Share) → 링크 복사(Copy link)',
        action: '링크 복사 (Copy link)',
        copyLink: true,
        finish: 'Notion 주소 전체를 복사하세요. 주소에서 ID를 따로 찾을 필요는 없습니다.',
        trouble: '표 안의 한 행이 아니라 정책표 전체 화면을 연 상태에서 주소를 복사하세요.',
    },
    {
        title: '주소 붙여넣고 연결 확인',
        instruction: '복지F5로 돌아와 아래의 정책표 주소 칸을 클릭하세요. Ctrl+V 또는 마우스 오른쪽 버튼 → 붙여넣기로 복사한 주소를 넣은 뒤 저장하고 정책표 연결 확인을 누르세요.',
        screen: '현재 설정 도우미의 입력칸',
        path: '정책 최신화용 정책표',
        secret: 'https://www.notion.so/내-정책표-…',
        action: '저장하고 정책표 연결 확인',
        finish: '정책표 연결 완료 안내가 표시되면 완료입니다.',
        appScreen: true,
        trouble: '확인에 실패하면 정책표의 연결 권한과 필수 15개 열을 먼저 확인하세요.',
    },
];

const ScreenLandmarks = ({ step }) => (
    <div className="text-xs leading-5 text-gray-500 dark:text-gray-400">
        <p className="font-medium text-gray-600 dark:text-gray-300">화면에서 그대로 찾을 이름</p>
        <p className="mt-2">{step.screen} · {step.path}</p>
        {step.permissions && (
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                {step.permissions.map(permission => <span key={permission} className="inline-flex items-center gap-1"><CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />{permission}</span>)}
            </div>
        )}
        <p className="mt-1">찾을 버튼: <span className="font-medium text-gray-700 dark:text-gray-300">{step.action}</span></p>
        {step.secret && <p className="mt-1 break-words">확인할 내용: {step.secret}</p>}
        <p className="mt-2">화면 언어에 따라 위치가 다를 수 있으니 메뉴와 버튼 이름을 기준으로 찾으세요.</p>
    </div>
);

// The unified journey owns navigation; instructions do not add a nested slideshow.
export const SetupStageInstructions = ({ kind, phase, templateHref }) => {
    const table = kind === 'news' ? NEWS_GALLERY_STEPS : POLICY_STEPS;
    const steps = phase === 'signup' ? NOTION_SIGNUP_STEPS
        : kind === 'google' ? GOOGLE_STEPS
        : kind === 'notion' ? NOTION_STEPS.slice(1)
        : phase === 'template' ? table.slice(0, 2) : table.slice(2);
    return <div data-setup-stage-instructions className="space-y-4">
        <SetupManualLink kind={kind} phase={phase || ((kind === 'policy' || kind === 'news') ? 'register' : undefined)} />
        <ol className="space-y-4">
            {steps.map((step, index) => <li key={step.title} className="rounded-xl border border-gray-200 bg-gray-50/60 p-4 dark:border-gray-700 dark:bg-gray-900/30">
                <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{index + 1}. {step.title}</h4>
                <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{step.instruction}</p>
                {step.href && <ExternalActionLink href={step.href} title={step.linkLabel} official tone="blue" className="mt-3" />}
                {phase === 'template' && index === 0 && (templateHref
                    ? <ExternalActionLink href={templateHref} title={kind === 'news' ? '새 소식 보관함 템플릿 열기' : '정책표 템플릿 열기'} official={false} tone={kind === 'news' ? 'emerald' : 'blue'} className="mt-3" />
                    : <p role="status" className="mt-3 text-sm text-amber-700">이 설치본에 템플릿 주소가 없습니다. 배포 담당자에게 주소를 확인해주세요. 이미 복제한 표가 있다면 다음 단계에 등록할 수 있습니다.</p>)}
                {step.copyLink && <p className="mt-3 rounded-lg bg-blue-50 p-3 text-sm text-blue-800 dark:bg-blue-950/40 dark:text-blue-200">웹에 게시하거나 공개 범위를 바꿀 필요는 없습니다. 내 표 전체 페이지에서 공유 → 링크 복사 후, 아래 입력칸에 Ctrl+V로 붙여넣으세요.</p>}
                {step.copyLink && <a href="https://www.notion.com/help/sharing-and-permissions" target="_blank" rel="noopener noreferrer" className="mt-3 inline-block text-sm text-blue-700 underline dark:text-blue-300">링크 복사와 공유 안내 · Notion 공식 도움말</a>}
            </li>)}
        </ol>
    </div>;
};

const SetupWalkthrough = ({ kind, defaultOpen, completed = false, inputId, nextLabel = '표', templateHref }) => {
    const isTableGuide = kind === 'policy' || kind === 'news';
    const steps = kind === 'google' ? GOOGLE_STEPS : kind === 'policy' ? POLICY_STEPS : kind === 'news' ? NEWS_GALLERY_STEPS : NOTION_STEPS;
    const [open, setOpen] = useState(defaultOpen ?? !completed);
    const [index, setIndex] = useState(0);
    const headingRef = useRef(null);
    const previousIndex = useRef(index);
    useLayoutEffect(() => {
        if (previousIndex.current === index) return;
        previousIndex.current = index;
        focusSetupStep(headingRef.current);
    }, [index]);
    const step = kind === 'notion' && index === steps.length - 1
        ? { ...steps[index], finish: `토큰 저장 안내가 표시되면 다음 단계에서 ${nextLabel}를 연결하세요.` }
        : steps[index];
    const helpTitle = { google: 'Gemini 키 만드는 방법', notion: 'Notion 연결 토큰 만드는 방법', policy: '정책표 준비 안내', news: '보관함 준비 안내' }[kind];
    const moveToInput = () => {
        const input = !completed && inputId ? document.getElementById(inputId) : null;
        if (input) {
            input.scrollIntoView({ block: 'center', behavior: 'auto' });
            input.focus({ preventScroll: true });
        } else setOpen(false);
    };
    const externalAction = step.href ? {href:step.href,title:step.linkLabel,official:true}
        : index === 0 && templateHref ? {href:templateHref,title:kind === 'policy' ? '정책표 템플릿 열기' : '새 소식 보관함 템플릿 열기',official:false} : null;
    const reference = step.copyLink
        ? {href:'https://www.notion.com/help/sharing-and-permissions',label:'링크 복사와 공유 안내 · Notion 공식 도움말'}
        : isTableGuide && index === 1
            ? {href:'https://developers.notion.com/guides/data-apis/working-with-databases',label:'연결 권한 안내 · Notion 공식 도움말'}
        : kind === 'google'
        ? {href:'https://blog.naver.com/rayi1004/224392953706',label:'Google API 키 발급 사진 안내 · 네이버 블로그'}
        : kind === 'notion'
            ? {href:'https://www.todoto.co.kr/guide_notion/?bmode=view&idx=170899562',label:'Notion API 키 발급 사진 안내 · 투두투'}
            : {href:'https://www.notion.com/ko/help/duplicate-public-pages',label:'템플릿 복제 안내 · Notion 공식 도움말'};

    return (
        <details data-setup-help open={open} onToggle={event => setOpen(event.currentTarget.open)} className={`w-full rounded-xl px-4 sm:px-5 ${isTableGuide?'bg-slate-50 ring-1 ring-inset ring-slate-200 dark:bg-gray-900/35 dark:ring-gray-700':'bg-gray-50/80 dark:bg-gray-900/35'}`}>
            <summary className="cursor-pointer list-none py-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">
                <span className="flex items-start justify-between gap-3">
                    <span className="min-w-0">
                        <span data-setup-help-label className={isTableGuide?'block text-sm font-semibold text-gray-800 dark:text-gray-100':'block text-xs font-medium text-gray-600 dark:text-gray-300'}>{helpTitle}</span>
                        {(!isTableGuide || completed) && <span className="mt-1 block text-xs leading-5 text-gray-500 dark:text-gray-400">{completed ? '필요할 때 다시 확인하세요.' : '이미 준비했다면 아래 입력칸에 바로 붙여넣으세요.'}</span>}
                    </span>
                    <span className="flex shrink-0 items-center gap-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
                        {open ? '접기' : '열기'}<ChevronDown aria-hidden="true" className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
                    </span>
                </span>
            </summary>
            <div className={isTableGuide?'pb-5 pt-1':'pb-5 pl-2 pt-1 sm:pl-3'}>
                <div className="space-y-3">
                    <SetupManualLink kind={kind} phase={kind === 'notion' && index === 0 ? 'signup' : isTableGuide && index >= 2 ? 'register' : undefined} />
                    <div>
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <p className={isTableGuide?'text-sm font-medium text-blue-700 dark:text-blue-300':'text-xs font-medium text-gray-500 dark:text-gray-400'}>{isTableGuide?`${index + 1}/${steps.length} · ${['템플릿 복제','연결 권한','링크 복사','붙여넣기·확인'][index]}`:`도움말 ${index + 1} / ${steps.length}`}</p>
                            {isTableGuide && index === 0 && !completed && inputId && <button type="button" onClick={moveToInput} className="min-h-9 rounded-lg px-2 text-sm text-blue-700 underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300">입력칸으로 이동 <ArrowRight aria-hidden="true" className="ml-1 inline h-4 w-4"/></button>}
                        </div>
                        <h4 data-setup-action ref={headingRef} tabIndex={-1} className={isTableGuide?'ui-subtitle mt-3 focus:outline-none':'mt-1.5 text-sm font-semibold leading-6 text-gray-700 dark:text-gray-300 focus:outline-none'}>{step.title}</h4>
                        <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{step.instruction}</p>
                        {externalAction && <ExternalActionLink {...externalAction} tone="blue" className="mt-3" />}
                        <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">{step.finish}</p>
                        {step.copyLink && <div className="mt-4 space-y-3 rounded-xl bg-white p-4 text-sm dark:bg-gray-800">
                            <ol aria-label="링크 복사 순서" className="flex flex-wrap items-center gap-2 font-medium text-gray-700 dark:text-gray-200">
                                <li>내 표 전체 페이지</li><li aria-hidden="true">→</li><li>공유</li><li aria-hidden="true">→</li><li>링크 복사</li>
                            </ol>
                            <p className="text-gray-700 dark:text-gray-200"><strong className="font-semibold">웹에 게시하거나 공개 범위를 바꿀 필요는 없습니다.</strong> 링크 복사만 눌러주세요.</p>
                            <p className="ui-meta">브라우저에서는 주소창을 클릭하고 Ctrl+L → Ctrl+C로 복사할 수도 있습니다. Notion 앱에서는 공유 → 링크 복사를 사용하세요.</p>
                        </div>}

                    </div>

                </div>
                <div className="mt-4 flex items-center justify-between gap-3">
                    <button type="button" onClick={() => setIndex(value => Math.max(0, value - 1))} disabled={index === 0} className="inline-flex min-h-10 items-center gap-1 rounded-lg px-3 text-sm font-medium text-gray-500 disabled:opacity-30"><ArrowLeft className="h-4 w-4" /> 이전 설명</button>
                    {index === steps.length - 1 ? (
                        <button type="button" onClick={moveToInput} className="inline-flex min-h-10 items-center gap-1 rounded-lg px-3 text-sm font-semibold text-indigo-700 hover:bg-indigo-50 focus-visible:ring-2 focus-visible:ring-indigo-500 dark:text-indigo-300 dark:hover:bg-indigo-950/40">{!completed && inputId ? '입력칸으로 이동' : '안내 접기'} <ArrowRight className="h-4 w-4" /></button>
                    ) : <button type="button" onClick={() => setIndex(value => Math.min(steps.length - 1, value + 1))} className="inline-flex min-h-10 items-center gap-1 rounded-lg px-3 text-sm font-semibold text-indigo-700 hover:bg-indigo-50 focus-visible:ring-2 focus-visible:ring-indigo-500 dark:text-indigo-300 dark:hover:bg-indigo-950/40">다음 설명 보기 <ArrowRight className="h-4 w-4" /></button>}
                </div>
            </div>
        </details>
    );
};

export default SetupWalkthrough;

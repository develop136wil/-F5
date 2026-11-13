// Development-only component fixture. All API methods used here are in-memory.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import '../../src/index.css';
import { welfareApi } from '../../src/api/welfare';
import NewsProfileEditor from '../../src/components/NewsProfileEditor';

const profile = { profile_id: 'fixture', revision: 1, name: '시험 사본', region: '전국',
    min_age_months: null, max_age_months: null, life_cycles: [], characteristics: ['한부모·조손'],
    interest_topics: ['생활지원'],
    keywords: ['지원'], excluded_keywords: [], site_ids: ['1'], sources: ['sites', 'bokjiro'],
    ai_prompt: '', semantic_text: '', relevance_enabled: true };
welfareApi.getNewsProfile = async () => ({ profile: structuredClone(profile) });
welfareApi.getNewsProfileTaxonomy = async () => ({
    source_url: 'https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52005M.do',
    life_cycles: ['임신·출산', '영유아', '아동', '청소년', '청년', '중장년', '노년'],
    household_situations: ['저소득', '장애인', '한부모·조손', '다자녀', '다문화·탈북민', '보훈대상자'],
    interest_topics: ['신체건강', '정신건강', '생활지원', '주거', '일자리', '문화·여가', '안전·위기', '임신·출산', '보육', '교육', '입양·위탁', '보호·돌봄', '서민금융', '법률', '에너지'],
    combination_rule: '같은 줄에서 여러 개를 고르면 그중 하나, 서로 다른 줄을 고르면 모든 줄에 맞는 자료를 찾습니다.',
    presets: [
        { id: 'senior', label: '노년', name: '노년 복지 소식', life_cycles: ['노년'], characteristics: [], interest_topics:['신체건강','생활지원','보호·돌봄','주거'], keywords: ['노년', '노인', '어르신', '고령'], semantic_text: '노년층 복지 지원 소식' },
        { id: 'single_parent', label: '한부모·조손', name: '한부모·조손 복지 소식', life_cycles: [], characteristics: ['한부모·조손'], interest_topics:['생활지원','주거','보육','교육'], keywords: ['한부모', '조손'], semantic_text: '한부모와 조손가족 복지 지원 소식' },
        { id: 'disabled', label: '장애인', name: '장애인 복지 소식', life_cycles: [], characteristics: ['장애인'], interest_topics:['신체건강','정신건강','생활지원','보호·돌봄'], keywords: ['장애인', '장애'], semantic_text: '장애인 복지 지원 소식' },
    ],
});
welfareApi.getSites = async () => ([
    {id:1,name:'도봉구청'}, {id:2,name:'도봉구육아종합지원센터'}, {id:3,name:'서울시 복지포털'},
]);
welfareApi.saveNewsProfile = async (_id, revision, settings) => {
    if (revision !== profile.revision) throw Error('버전 충돌');
    Object.assign(profile, structuredClone(settings), { revision: revision + 1 });
};
welfareApi.previewNewsTarget = async (_settings, facts) => ({
    status: facts.target_characteristics.includes('한부모') ? 'match' : 'review',
    checks: { characteristics: facts.target_characteristics.includes('한부모') ? 'match' : 'review' },
});
welfareApi.previewBokjiroSample = async settings => ({
    state: 'finished', matched: 2, scanned: 9, pages_failed: 0, saved: false, ai_called: false,
    items: [
        { service_id: 'sample-1', title: `${settings.name} 표본 1`, provider: '보건복지부',
          link: 'https://www.bokjiro.go.kr/', life_cycles: settings.life_cycles,
          household_situations: settings.characteristics, interest_topics: ['생활지원'] },
        { service_id: 'sample-2', title: `${settings.name} 표본 2`, provider: '서울특별시',
          link: 'https://www.bokjiro.go.kr/', life_cycles: settings.life_cycles,
          household_situations: settings.characteristics, interest_topics: ['보호·돌봄'] },
    ],
});
function Fixture() {
    const [message, setMessage] = useState('');
    const [editing, setEditing] = useState(true);
    return <main className="mx-auto max-w-5xl p-6">
        <p className="mb-4">격리 UI 테스트 · 모의 응답만 사용 · 실제 데이터/AI 연결 없음</p>
        {editing ? <NewsProfileEditor profileId="fixture" onClose={() => { setEditing(false); setMessage('편집 닫기 확인'); }}
            onSaved={() => { setEditing(false); setMessage(`저장 확인: ${profile.name}, 버전 ${profile.revision}`); }} />
            : <button onClick={() => setEditing(true)}>저장한 기준 다시 열기</button>}
        <p role="status">{message}</p>
    </main>;
}
createRoot(document.getElementById('root')).render(<MemoryRouter><Fixture /></MemoryRouter>);
